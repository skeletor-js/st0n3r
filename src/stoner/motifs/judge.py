"""Advisory LLM layer over the deterministic motif scans.

Two judgments, both categorical/comparative and both advisory only -- they
emit `Finding`s a human triages and NEVER write `canon/motifs.md`
(invariants 1, 3):

- `judge_candidates` triages mined n-gram candidates as promote/ignore.
- `judge_rhyme` reads ONLY the opening and closing windows (never the middle,
  invariant 4) and returns a comparative RHYMES/PARTIAL/FLAT verdict with
  quoted image pairs as evidence.

Both resolve against the `reviewer` role via `pipelines/common.call_model`
(one plain no-tools completion each -- text-only providers are fine) and parse
with `review.passes.extract_json`. A failed call or unparseable output
degrades to a single info `Finding` rather than raising, mirroring
`review/runner.py`.
"""

from __future__ import annotations

import json

from ..canon.store import CanonStore, MotifRow
from ..pipelines.common import call_model, render_prompt
from ..project import WritingProject
from ..providers.base import Provider, ProviderError
from ..review.passes import extract_json
from ..types import Finding, Severity, Usage
from .scan import CandidateReport, RhymeReport, window_bodies

#: Cap on findings emitted per judgment (mirrors `_MAX_FINDINGS_PER_PASS`).
_MAX_FINDINGS = 25
_RHYME_VERDICTS = ("RHYMES", "PARTIAL", "FLAT")


def _info_finding(source: str, issue: str) -> Finding:
    """The degrade-to-advisory fallback: one info Finding, never an exception."""
    return Finding(source=source, severity=Severity.info, category="unavailable", issue=issue)


def _registry_block(store: CanonStore) -> str:
    rows: list[MotifRow] = store.motifs()
    if not rows:
        return "(no motifs registered yet)"
    return "\n".join(
        f"- {r.motif}: {r.meaning or '(no stated meaning)'} "
        f"[anchors: {'; '.join(r.anchor_list()) or 'none'}]"
        for r in rows
    )


# ---------------------------------------------------------------------------
# Candidate triage
# ---------------------------------------------------------------------------


def _candidates_user(report: CandidateReport, store: CanonStore) -> str:
    lines = [
        "## Registered motifs\n",
        _registry_block(store),
        "\n## Mined candidate phrases (deterministic; recur across chapters)\n",
    ]
    for c in report.candidates:
        lines.append(f"- \"{c.gram}\" — chapters {c.chapters}, {c.total} occurrence(s)")
    lines.append(
        "\n## Task\n\nFor each candidate, decide promote or ignore. Return STRICT "
        "JSON only:\n"
        + json.dumps(
            {
                "candidates": [
                    {
                        "gram": "<the candidate phrase, verbatim>",
                        "verdict": "promote|ignore",
                        "suggested_name": "<short motif name if promote>",
                        "meaning": "<what it would mean if promote>",
                        "reason": "<one sentence>",
                    }
                ]
            }
        )
    )
    return "\n".join(lines)


def judge_candidates(
    project: WritingProject,
    report: CandidateReport,
    store: CanonStore,
    model: str | None = None,
    provider: Provider | None = None,
) -> tuple[list[Finding], Usage]:
    """Triage mined candidates into advisory promote/ignore Findings."""
    usage = Usage()
    if not report.candidates:
        return [], usage
    system = render_prompt("motif_candidates.md", {"project_name": project.config.project_name})
    user = _candidates_user(report, store)
    try:
        text, usage = call_model(
            project, "reviewer", system=system, user=user, model=model, provider=provider
        )
    except ProviderError as e:
        return [_info_finding("motif:candidates", f"candidate triage unavailable: {e}")], usage

    data = extract_json(text)
    items = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [_info_finding("motif:candidates", "candidate triage returned no usable JSON")], usage

    findings: list[Finding] = []
    for item in items[:_MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict", "")).strip().lower()
        gram = str(item.get("gram", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if not gram:
            continue
        if verdict == "promote":
            name = str(item.get("suggested_name", "")).strip()
            meaning = str(item.get("meaning", "")).strip()
            issue = f"promote '{gram}' as a motif" + (f" ({name})" if name else "")
            if reason:
                issue += f" -- {reason}"
            suggestion = (
                f"register with `stoner motifs add` (meaning: {meaning})"
                if meaning
                else "register with `stoner motifs add`"
            )
            findings.append(
                Finding(
                    source="motif:candidates",
                    severity=Severity.minor,
                    category="promote",
                    quote=gram,
                    issue=issue,
                    suggestion=suggestion,
                )
            )
        elif verdict == "ignore":
            issue = f"ignore '{gram}'" + (f" -- {reason}" if reason else "")
            findings.append(
                Finding(
                    source="motif:candidates",
                    severity=Severity.info,
                    category="ignore",
                    quote=gram,
                    issue=issue,
                )
            )
    return findings, usage


# ---------------------------------------------------------------------------
# Rhyme verdict
# ---------------------------------------------------------------------------


def _rhyme_user(
    report: RhymeReport, store: CanonStore, opening_text: str, closing_text: str
) -> str:
    return "\n".join(
        [
            "## Registered motifs\n",
            _registry_block(store),
            "\n## Deterministic overlap (already computed)\n",
            f"- content-token Jaccard: {report.jaccard}",
            f"- shared distinctive terms: {', '.join(report.shared_distinctive) or 'none'}",
            f"- motifs present in both windows: {', '.join(report.motifs_both) or 'none'}",
            f"- motifs opening-only: {', '.join(report.motifs_open_only) or 'none'}",
            f"- motifs closing-only: {', '.join(report.motifs_close_only) or 'none'}",
            f"\n## Opening window (chapters {report.opening_chapters})\n",
            opening_text.strip() or "(empty)",
            f"\n## Closing window (chapters {report.closing_chapters})\n",
            closing_text.strip() or "(empty)",
            "\n## Task\n\nDoes the ending rhyme with the opening? Which opening "
            "images/promises recur or transform in the ending? Return STRICT JSON "
            "only:\n"
            + json.dumps(
                {
                    "verdict": "RHYMES|PARTIAL|FLAT",
                    "pairs": [
                        {
                            "opening_quote": "<verbatim from the opening window>",
                            "closing_quote": "<verbatim from the closing window>",
                            "note": "<how the second answers the first>",
                        }
                    ],
                }
            ),
        ]
    )


def judge_rhyme(
    project: WritingProject,
    report: RhymeReport,
    store: CanonStore,
    window: int = 1,
    model: str | None = None,
    provider: Provider | None = None,
) -> tuple[list[Finding], Usage]:
    """Comparative RHYMES/PARTIAL/FLAT verdict over the opening/closing windows.

    The user prompt carries ONLY the opening and closing windows (never any
    middle chapter, invariant 4). Degrades to a single info Finding on failure.
    """
    usage = Usage()
    _, _open_nums, _close_nums, opening_text, closing_text, _mid = window_bodies(project, window)
    system = render_prompt("motif_rhyme.md", {"project_name": project.config.project_name})
    user = _rhyme_user(report, store, opening_text, closing_text)
    try:
        text, usage = call_model(
            project, "reviewer", system=system, user=user, model=model, provider=provider
        )
    except ProviderError as e:
        return [_info_finding("motif:rhyme", f"rhyme judgment unavailable: {e}")], usage

    data = extract_json(text)
    verdict = str(data.get("verdict", "")).strip().upper() if isinstance(data, dict) else ""
    if verdict not in _RHYME_VERDICTS:
        return [_info_finding("motif:rhyme", "rhyme judgment returned no usable verdict")], usage

    severity = {
        "RHYMES": Severity.info,
        "PARTIAL": Severity.minor,
        "FLAT": Severity.major,
    }[verdict]
    findings: list[Finding] = [
        Finding(
            source="motif:rhyme",
            severity=severity,
            category=verdict.lower(),
            issue=f"ending/opening rhyme verdict: {verdict}",
        )
    ]
    pairs = data.get("pairs")
    if isinstance(pairs, list):
        for pair in pairs[:_MAX_FINDINGS]:
            if not isinstance(pair, dict):
                continue
            oq = str(pair.get("opening_quote", "")).strip()
            cq = str(pair.get("closing_quote", "")).strip()
            note = str(pair.get("note", "")).strip()
            if not oq and not cq:
                continue
            issue = f"opening {oq!r} <-> closing {cq!r}" + (f" -- {note}" if note else "")
            findings.append(
                Finding(
                    source="motif:rhyme",
                    severity=Severity.info,
                    category=f"{verdict.lower()}:pair",
                    quote=oq,
                    issue=issue,
                    suggestion=cq,
                )
            )
    return findings, usage
