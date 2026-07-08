"""Pure-arithmetic attention heatmap from saved chapter logs.

Every number here is computed in Python -- there is no model call in
aggregation (invariant 1). From a `RunState` plus the chapter bodies it split
markers into paragraph segments (offsets against the original body, so segments
survive report re-renders), and per segment computes: marker counts by type, an
attention value (hooked fraction minus bored+confused fraction), an agreement
fraction (share of the roster that marked the segment), and disagreement
patterns found by re-aggregating over persona-attribute subsets (age band,
patience, genre prior).

Segments whose negative-marker agreement meets `readers.agreement_threshold`
become advisory `Finding`s (source `readers:heatmap`, severity major, span
attached) that gate nothing (invariant 2). That high-agreement set is mirrored
into `.stoner/reviews/` as a saved report pair (`readers-<run_id>-<ts>.json` +
`.md`, top-level `kind: "readers"`) holding only those findings as standard
`Finding` objects, so the existing finding-triage UI and PATCH-status endpoint
cover them with no new write surface. Finding ids are deterministic
(`rh-<chapter>-<segment>`) so re-aggregating the same state is byte-identical
(modulo timestamps) and a PATCHed status targets a stable id.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..ledger import Ledger
from ..project import WritingProject
from ..types import Finding, Severity, Span
from .personas import Persona, load_personas
from .state import NEGATIVE_MARKERS, RunState, load_state, run_dir

#: Subset attention gap (max - min across an attribute's values) that flags a
#: segment as a disagreement pattern.
DISAGREEMENT_DELTA = 0.5

_MARKER_TYPES: tuple[str, ...] = ("hooked", "bored", "confused", "reread")
_ATTRIBUTES: tuple[str, ...] = ("age band", "patience", "genre")


@dataclass
class Segment:
    chapter: int
    index: int  # 0-based paragraph index within the chapter
    start: int
    end: int
    line: int
    quote: str
    counts: dict[str, int]
    attention: float
    agreement: float
    negative_agreement: float
    disagreements: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class HeatmapReport:
    run_id: str
    roster_size: int
    segments: list[Segment] = field(default_factory=list)
    chapter_markers: dict[int, dict[str, int]] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Segmentation (offset-preserving)
# ---------------------------------------------------------------------------


def paragraph_segments(body: str) -> list[tuple[int, int, int, str]]:
    """Split `body` into non-empty paragraphs as (index, start, end, text),
    with char offsets into the original body (blank-line delimited)."""
    out: list[tuple[int, int, int, str]] = []
    idx = 0
    pos = 0
    n = len(body)
    while pos < n:
        # skip leading blank space / newlines
        while pos < n and body[pos] in "\r\n":
            pos += 1
        if pos >= n:
            break
        start = pos
        # a paragraph runs until a blank line (\n\n) or EOF
        nl = body.find("\n\n", pos)
        end = nl if nl != -1 else n
        text = body[start:end].rstrip()
        real_end = start + len(text)
        if text.strip():
            out.append((idx, start, real_end, text))
            idx += 1
        pos = end + 2 if nl != -1 else n
    return out


def _quote(text: str, limit: int = 60) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _attr_value(p: Persona, attr: str) -> str:
    if attr == "age band":
        return p.age_band
    if attr == "patience":
        return p.patience
    return p.primary_genre


def _subset_attention(
    personas_by_type: dict[str, set[str]], members: set[str]
) -> float | None:
    """Attention over one subset of the roster: (hooked - bored - confused)
    counted among `members`, divided by the subset size. None if empty."""
    if not members:
        return None
    hooked = len(personas_by_type["hooked"] & members)
    neg = len((personas_by_type["bored"] | personas_by_type["confused"]) & members)
    return (hooked - neg) / len(members)


def _disagreements(
    personas_by_type: dict[str, set[str]], roster: list[str], personas: dict[str, Persona]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for attr in _ATTRIBUTES:
        groups: dict[str, set[str]] = {}
        for pid in roster:
            p = personas.get(pid)
            if p is None:
                continue
            groups.setdefault(_attr_value(p, attr), set()).add(pid)
        split: dict[str, float] = {}
        for value, members in groups.items():
            att = _subset_attention(personas_by_type, members)
            if att is not None:
                split[value] = round(att, 3)
        if len(split) < 2:
            continue
        delta = max(split.values()) - min(split.values())
        if delta >= DISAGREEMENT_DELTA:
            out.append({"attribute": attr, "delta": round(delta, 3), "split": split})
    return out


def build_heatmap(
    state: RunState,
    chapter_bodies: dict[int, str],
    personas: dict[str, Persona],
    *,
    agreement_threshold: float,
) -> HeatmapReport:
    """Aggregate a run's chapter logs into a `HeatmapReport`. Pure: no I/O."""
    roster = list(state.roster)
    roster_size = len(roster)
    report = HeatmapReport(run_id=state.run_id, roster_size=roster_size)
    total_markers = 0
    any_disagreement = False

    for chapter in sorted(state.chapter_logs):
        log = state.chapter_logs[chapter]
        body = chapter_bodies.get(chapter, "")
        segs = paragraph_segments(body)
        # personas_by_type per segment index; plus a chapter-level bucket for
        # null-span markers.
        by_seg: dict[int, dict[str, set[str]]] = {
            i: {t: set() for t in _MARKER_TYPES} for i, *_ in segs
        }
        chapter_bucket: dict[str, int] = {t: 0 for t in _MARKER_TYPES}

        for m in log.markers:
            total_markers += 1
            if m.span is None:
                chapter_bucket[m.type] += 1
                continue
            seg_index = _segment_of(segs, m.span.start)
            if seg_index is None:
                chapter_bucket[m.type] += 1
                continue
            by_seg[seg_index][m.type].add(m.persona)

        if any(v for v in chapter_bucket.values()):
            report.chapter_markers[chapter] = chapter_bucket

        for i, start, end, text in segs:
            pbt = by_seg[i]
            counts = {t: len(pbt[t]) for t in _MARKER_TYPES}
            marked = set().union(*pbt.values()) if roster_size else set()
            if roster_size:
                hooked_frac = counts["hooked"] / roster_size
                neg_frac = (counts["bored"] + counts["confused"]) / roster_size
                attention = hooked_frac - neg_frac
                agreement = len(marked) / roster_size
                neg_marked = pbt["bored"] | pbt["confused"]
                negative_agreement = len(neg_marked) / roster_size
            else:
                attention = agreement = negative_agreement = 0.0
            dis = _disagreements(pbt, roster, personas) if roster_size else []
            if dis:
                any_disagreement = True
            report.segments.append(
                Segment(
                    chapter=chapter,
                    index=i,
                    start=start,
                    end=end,
                    line=body.count("\n", 0, start) + 1,
                    quote=_quote(text),
                    counts=counts,
                    attention=round(attention, 3),
                    agreement=round(agreement, 3),
                    negative_agreement=round(negative_agreement, 3),
                    disagreements=dis,
                )
            )
            if negative_agreement >= agreement_threshold and negative_agreement > 0:
                report.findings.append(_finding_for(chapter, i, start, end, body, text, counts, negative_agreement))

    if total_markers and not any_disagreement:
        report.notes.append(
            "no attribute disagreement anywhere in the run -- the roster may be "
            "collapsing into one voice; treat unanimous reactions with suspicion"
        )
    if not total_markers:
        report.notes.append("no markers recorded; heatmap is empty")
    return report


def _segment_of(segs: list[tuple[int, int, int, str]], offset: int) -> int | None:
    for i, start, end, _text in segs:
        if start <= offset < end:
            return i
        if offset == end and end == start:  # zero-length edge
            return i
    # fall back: an offset past a paragraph's rstripped end but before the next
    # paragraph belongs to that paragraph.
    for i, start, end, _text in segs:
        if start <= offset <= end:
            return i
    return None


def _finding_for(
    chapter: int,
    index: int,
    start: int,
    end: int,
    body: str,
    text: str,
    counts: dict[str, int],
    negative_agreement: float,
) -> Finding:
    neg_bits = ", ".join(f"{counts[t]} {t}" for t in NEGATIVE_MARKERS if counts[t])
    return Finding(
        id=f"rh-{chapter:02d}-{index}",
        source="readers:heatmap",
        severity=Severity.major,
        category=f"ch-{chapter:02d}:segment-{index}",
        span=Span(start=start, end=end, line=body.count("\n", 0, start) + 1),
        quote=_quote(text),
        issue=(
            f"{round(negative_agreement * 100)}% of readers checked out here "
            f"({neg_bits})."
        ),
        suggestion="tighten or cut this passage; it loses a large share of the roster",
    )


# ---------------------------------------------------------------------------
# Payload / rendering / persistence
# ---------------------------------------------------------------------------


def _segment_payload(s: Segment) -> dict[str, Any]:
    return {
        "chapter": s.chapter,
        "index": s.index,
        "start": s.start,
        "end": s.end,
        "line": s.line,
        "quote": s.quote,
        "counts": s.counts,
        "attention": s.attention,
        "agreement": s.agreement,
        "negative_agreement": s.negative_agreement,
        "disagreements": s.disagreements,
    }


def to_payload(report: HeatmapReport) -> dict[str, Any]:
    """The JSON shape saved to `<run>/heatmap.json`. `kind: "readers"` marks the
    report; `segments` is the timeline the UI panel renders."""
    return {
        "kind": "readers",
        "run_id": report.run_id,
        "created_at": report.created_at,
        "roster_size": report.roster_size,
        "segments": [_segment_payload(s) for s in report.segments],
        "chapter_markers": {str(k): v for k, v in report.chapter_markers.items()},
        "findings": [f.model_dump(mode="json") for f in report.findings],
        "notes": report.notes,
    }


def render_markdown(report: HeatmapReport) -> str:
    lines = [
        "# Reader attention heatmap",
        "",
        f"**Run:** {report.run_id}  ",
        f"**Roster:** {report.roster_size} readers  ",
        f"**Segments:** {len(report.segments)}  ",
        f"**Trouble segments (findings):** {len(report.findings)}",
        "",
    ]
    by_chapter: dict[int, list[Segment]] = {}
    for s in report.segments:
        by_chapter.setdefault(s.chapter, []).append(s)
    for chapter in sorted(by_chapter):
        lines.append(f"## Chapter {chapter}")
        lines.append("")
        lines.append("| seg | attention | agreement | hooked | bored | confused | reread | quote |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for s in by_chapter[chapter]:
            c = s.counts
            lines.append(
                f"| {s.index} | {s.attention:+.2f} | {s.agreement:.2f} | "
                f"{c['hooked']} | {c['bored']} | {c['confused']} | {c['reread']} | {s.quote} |"
            )
        lines.append("")
    if report.findings:
        lines += ["## Trouble segments", ""]
        for f in report.findings:
            lines.append(f"- **[{f.category}]** {f.issue} — `{f.quote}`")
        lines.append("")
    dis = [s for s in report.segments if s.disagreements]
    if dis:
        lines += ["## Disagreement patterns", ""]
        for s in dis:
            for d in s.disagreements:
                split = ", ".join(f"{k}: {v:+.2f}" for k, v in d["split"].items())
                lines.append(f"- ch {s.chapter} seg {s.index}: split by **{d['attribute']}** (Δ{d['delta']:.2f}) — {split}")
        lines.append("")
    if report.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in report.notes]
    return "\n".join(lines).rstrip() + "\n"


def save_heatmap(project: WritingProject, report: HeatmapReport) -> dict[str, str]:
    """Write `heatmap.json` + `report.md` into the run dir and mirror the
    high-agreement findings into `.stoner/reviews/` as a `kind: "readers"`
    report. Ledgers `readers.heatmap`. Returns the written paths."""
    rdir = run_dir(project, report.run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    heatmap_path = rdir / "heatmap.json"
    report_md_path = rdir / "report.md"
    heatmap_path.write_text(json.dumps(to_payload(report), indent=2, ensure_ascii=False), encoding="utf-8")
    report_md_path.write_text(render_markdown(report), encoding="utf-8")

    paths = {"heatmap": str(heatmap_path), "report": str(report_md_path)}

    if report.findings:
        ts = int(report.created_at)
        reviews = project.root / ".stoner" / "reviews"
        reviews.mkdir(parents=True, exist_ok=True)
        stem = f"readers-{report.run_id}-{ts}"
        mirror = {
            "kind": "readers",
            "run_id": report.run_id,
            "path": "",
            "created_at": report.created_at,
            "summary": f"{len(report.findings)} high-agreement trouble segment(s) from reader run {report.run_id}",
            "findings": [f.model_dump(mode="json") for f in report.findings],
        }
        json_path = reviews / f"{stem}.json"
        md_path = reviews / f"{stem}.md"
        json_path.write_text(json.dumps(mirror, indent=2, ensure_ascii=False), encoding="utf-8")
        md_path.write_text(render_markdown(report), encoding="utf-8")
        paths["review_json"] = str(json_path)
        paths["review_md"] = str(md_path)

    Ledger(project.root).append(
        "readers.heatmap",
        target=report.run_id,
        segments=len(report.segments),
        findings=len(report.findings),
    )
    return paths


def run_heatmap(project: WritingProject, run_id: str) -> tuple[HeatmapReport, dict[str, str]]:
    """Load a run's state + chapter bodies, aggregate, and persist. The one
    entry point the CLI and any caller use."""
    state = load_state(project, run_id)
    if not state.chapter_logs and not state.roster:
        raise ValueError(f"no run state for {run_id!r} (nothing to aggregate)")
    bodies: dict[int, str] = {}
    for chapter in state.chapter_logs:
        try:
            _, body = project.read_chapter(chapter)
        except Exception:  # noqa: BLE001 - a since-deleted chapter aggregates as empty
            body = ""
        bodies[chapter] = body
    personas = load_personas(project)
    report = build_heatmap(
        state, bodies, personas, agreement_threshold=project.config.readers.agreement_threshold
    )
    paths = save_heatmap(project, report)
    return report, paths
