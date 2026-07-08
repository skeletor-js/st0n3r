"""Cast pipeline stages: the model calls the pure curator/boundedness modules
cannot make themselves.

Mirrors `pipelines/write.py:run_archive`: the pure module (curator /
boundedness) does prompt assembly, parsing, and the deterministic diff; this
module owns the `call_model` invocation, the disk writes, and the ledger
entry. Every mutating action ledgers under the `cast.*` prefix.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from ..ledger import Ledger
from ..pipelines.common import call_model, render_prompt
from ..project import WritingProject
from ..providers.base import Provider
from ..types import Finding, Usage
from .boundedness import attribution_prompt, check_boundedness, parse_attributions
from .curator import CastApplyResult, apply_cast_update, cast_update_prompt, parse_cast_update
from .store import CastStore, review_digest

_DIGEST_CHARS = 8000


class CastReport(BaseModel):
    """Saved boundedness report. `kind` is the repo-wide report discriminator."""

    kind: str = "cast"
    path: str
    chapter: int
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""
    model: str = ""
    usage: Usage = Field(default_factory=Usage)
    created_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Curator stage
# ---------------------------------------------------------------------------


def run_cast_update(
    project: WritingProject,
    number: int,
    model: str | None = None,
    provider: Provider | None = None,
    auto: bool = False,
) -> CastApplyResult:
    """Extract private-state updates from chapter `number`, diff, apply.

    No cast sheets -> a no-op result (no model call). Ledgers `cast.update`
    with applied/conflict counts.
    """
    store = CastStore(project)
    if not store.list_slugs():
        return CastApplyResult(chapter=number, dry_run=not auto)

    _, body = project.read_chapter(number)
    digest = review_digest(project, number, max_chars=_DIGEST_CHARS)
    system = render_prompt("cast_update.md", {"project_name": project.config.project_name, "chapter_number": f"{number:02d}"})
    user = cast_update_prompt(body, digest)
    text, _usage = call_model(project, "archivist", system=system, user=user, model=model, provider=provider)
    parsed = parse_cast_update(text)
    result = apply_cast_update(store, parsed, number, auto=auto)

    Ledger(project.root).append(
        "cast.update",
        target=project.chapter_rel(number),
        applied=len(result.applied),
        conflicts=len(result.conflicts),
        auto=auto,
    )
    return result


# ---------------------------------------------------------------------------
# Boundedness stage
# ---------------------------------------------------------------------------


def run_cast_check(
    project: WritingProject,
    number: int,
    model: str | None = None,
    provider: Provider | None = None,
) -> CastReport:
    """Attribute chapter references to ledger entries, run the pure check, save.

    Findings are advisory and never gate. No cast sheets -> empty report with a
    note, no model call. Saves `.stoner/reviews/cast-ch-NN-<ts>.json` + `.md`
    and ledgers `cast.check`.
    """
    store = CastStore(project)
    sheets = store.list_sheets()
    rel = project.chapter_rel(number)

    if not sheets:
        report = CastReport(path=rel, chapter=number, summary="no cast sheets; nothing to check")
        _save_report(project, number, report)
        Ledger(project.root).append("cast.check", target=rel, findings=0)
        return report

    _, body = project.read_chapter(number)
    digest = review_digest(project, number, max_chars=_DIGEST_CHARS)
    system = render_prompt("cast_attribution.md", {"project_name": project.config.project_name, "chapter_number": f"{number:02d}"})
    user = attribution_prompt(body, digest)
    text, usage = call_model(project, "archivist", system=system, user=user, model=model, provider=provider)

    attributions = parse_attributions(text)
    findings = check_boundedness(attributions, sheets, number, body=body)
    violations = sum(1 for f in findings if f.category == "anachronistic-knowledge")

    report = CastReport(
        path=rel,
        chapter=number,
        findings=findings,
        summary=f"{violations} boundedness violation(s), {len(findings)} finding(s) total",
        model=model or project.config.models.archivist,
        usage=usage,
    )
    _save_report(project, number, report)
    Ledger(project.root).append("cast.check", target=rel, findings=len(findings), violations=violations)
    return report


def _save_report(project: WritingProject, number: int, report: CastReport) -> None:
    ts = int(time.time())
    base = f".stoner/reviews/cast-ch-{number:02d}-{ts}"
    project.write(f"{base}.json", report.model_dump_json(indent=2))
    project.write(f"{base}.md", _render_markdown(report))


def _render_markdown(report: CastReport) -> str:
    order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
    findings = sorted(report.findings, key=lambda f: order.get(f.severity.value, 4))
    lines = [
        f"# Cast boundedness report: {report.path}",
        "",
        f"**Model:** {report.model}  ",
        f"**Chapter:** {report.chapter}",
        "",
        "## Summary",
        "",
        report.summary or "_No summary._",
        "",
        f"## Findings ({len(findings)})",
        "",
    ]
    if not findings:
        lines.append("_No findings._")
    for f in findings:
        loc = f"L{f.span.line}" if f.span else "-"
        quote = f" -- `{f.quote}`" if f.quote else ""
        lines.append(f"- **[{f.severity.value}]** ({f.category}) {loc}: {f.issue}{quote}")
        if f.suggestion:
            lines.append(f"  - suggestion: {f.suggestion}")
    return "\n".join(lines)
