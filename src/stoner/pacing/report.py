"""PacingReport: the per-book pacing report and its three renderers.

The report's spine is the per-chapter `series` table -- one row per chapter,
one column per instrument signal -- because that is exactly what the UI
timeline overlay renders and what a writer scans. Findings hang off it for
the anomalies (flatline runs, ending echoes, cadence breaks, scene-starved
chapters).

`render(report, fmt)` is pure (detached rich Console, returns text) with
the same three formats as `slop/report.py`. The JSON payload carries a
top-level `kind: "pacing"` (the repo-wide report discriminator consumed by
`ui/server.py:_review_kind`) and a top-level `series` key (consumed by the
UI Pacing panel).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..types import Finding, Usage
from .judge import ChapterJudgment

_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}


@dataclass
class PacingReport:
    """Result of one `run_pacing` over the whole manuscript. Feature-local,
    like `BookReviewReport` -- it crosses no module boundary that would
    justify `types.py`."""

    series: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    flatlines: list[tuple[int, int]] = field(default_factory=list)
    llm: bool = False
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    created_at: float = field(default_factory=time.time)
    json_path: str = ""
    md_path: str = ""


# ---------------------------------------------------------------------------
# Aggregation: flatline runs (arithmetic over labels + empty ledgers)
# ---------------------------------------------------------------------------


def derive_flatlines(
    judgments: list[ChapterJudgment], min_run: int
) -> list[tuple[int, int]]:
    """Maximal runs of >= `min_run` consecutive chapters where tension is
    holds/sags AND nothing changes hands, as (first, last) chapter numbers.

    This is arithmetic over the judge's labels, not a judgment itself --
    the "Chapters 9-12 flatline" diagnostic is computed, never asked for.
    """
    runs: list[tuple[int, int]] = []
    current: list[int] = []
    for j in judgments:
        flat = j.tension in ("holds", "sags") and not j.changes_hands
        if flat:
            current.append(j.chapter)
        else:
            if len(current) >= min_run:
                runs.append((current[0], current[-1]))
            current = []
    if len(current) >= min_run:
        runs.append((current[0], current[-1]))
    return runs


def flatline_finding(first: int, last: int) -> Finding:
    from ..types import Severity

    return Finding(
        source="pacing:flatline",
        severity=Severity.major,
        category=f"ch-{first:02d}:flatline",
        issue=f"Chapters {first}–{last} flatline; nothing changes hands.",
        suggestion=(
            "give at least one of these chapters an irreversible change -- "
            "stakes, possession, knowledge, or allegiance must move"
        ),
    )


# ---------------------------------------------------------------------------
# Payload / rendering
# ---------------------------------------------------------------------------


def to_payload(report: PacingReport) -> dict[str, Any]:
    """The JSON shape saved to `.stoner/reviews/pacing-<ts>.json`. Carries
    `kind: "pacing"` (report discriminator) and a top-level `series`."""
    return {
        "kind": "pacing",
        "created_at": report.created_at,
        "llm": report.llm,
        "model": report.model,
        "series": report.series,
        "flatlines": [list(run) for run in report.flatlines],
        "stats": report.stats,
        "usage": report.usage.model_dump(),
        "findings": [f.model_dump(mode="json") for f in report.findings],
    }


def render(report: PacingReport, fmt: str = "rich") -> str:
    """Render a PacingReport as ``"rich"``, ``"markdown"``, or ``"json"`` text."""
    if fmt == "json":
        return json.dumps(to_payload(report), indent=2, ensure_ascii=False)
    if fmt == "markdown":
        return _render_markdown(report)
    if fmt == "rich":
        return _render_rich(report)
    raise ValueError(f"unknown render format: {fmt!r} (expected rich|markdown|json)")


def _fmt_ratio(value: Any) -> str:
    return f"{value:.0%}" if isinstance(value, (int, float)) else "-"


def _row_cells(row: dict[str, Any]) -> list[str]:
    changes = row.get("changes_hands")
    beats = row.get("beats")
    return [
        f"{row.get('chapter', 0):02d}",
        f"{row.get('words', 0):,}",
        _fmt_ratio(row.get("in_scene_fraction")),
        "/".join(
            _fmt_ratio(row.get(k)) for k in ("dialogue_ratio", "interiority_ratio", "action_ratio")
        ),
        str(row.get("pov") or "-"),
        str(row.get("ending_shape", "-")).replace("_", " "),
        str(row.get("tension", "-")),
        ("-" if changes is None else str(len(changes))),
        ("-" if beats is None else "/".join(str(beats.get(k, 0)) for k in ("landed", "drifted", "missed"))),
    ]


_SERIES_HEADERS = (
    "ch", "words", "in-scene", "dia/int/act", "pov", "ending", "tension",
    "changes", "beats l/d/m",
)


def _render_markdown(report: PacingReport) -> str:
    lines = [
        "# Pacing report",
        "",
        f"**Chapters:** {len(report.series)}  ",
        f"**LLM judge:** {'on (' + report.model + ')' if report.llm else 'off'}  ",
        f"**Flatline runs:** {len(report.flatlines) or '-'}",
        "",
        "## Series",
        "",
        "| " + " | ".join(_SERIES_HEADERS) + " |",
        "|" + "---|" * len(_SERIES_HEADERS),
    ]
    for row in report.series:
        lines.append("| " + " | ".join(_row_cells(row)) + " |")

    findings = sorted(report.findings, key=lambda f: _SEVERITY_ORDER.get(f.severity.value, 4))
    lines += ["", f"## Findings ({len(findings)})", ""]
    if not findings:
        lines.append("_No findings._")
    for f in findings:
        quote = f" -- `{f.quote}`" if f.quote else ""
        lines.append(f"- **[{f.severity.value}]** ({f.category}) {f.issue}{quote}")
        if f.suggestion:
            lines.append(f"  - suggestion: {f.suggestion}")

    lines += [
        "",
        "## Stats",
        "",
        "```json",
        json.dumps(report.stats, indent=2, default=str),
        "```",
    ]
    return "\n".join(lines)


def _render_rich(report: PacingReport) -> str:
    import io

    from rich.console import Console
    from rich.table import Table

    # Detached buffer: render() must be pure (no direct stdout writes).
    console = Console(record=True, width=120, file=io.StringIO(), force_terminal=True)
    console.print("[bold]Pacing report[/bold]")
    console.print(
        f"chapters: [bold]{len(report.series)}[/bold]   "
        f"llm judge: [bold]{'on' if report.llm else 'off'}[/bold]"
        + (f" ({report.model})" if report.llm and report.model else "")
    )
    if report.flatlines:
        for first, last in report.flatlines:
            console.print(f"[bold red]Chapters {first}–{last} flatline; nothing changes hands.[/bold red]")

    table = Table(title="Series")
    for col in _SERIES_HEADERS:
        table.add_column(col)
    for row in report.series:
        table.add_row(*_row_cells(row))
    console.print(table)

    findings = sorted(report.findings, key=lambda f: _SEVERITY_ORDER.get(f.severity.value, 4))
    f_table = Table(title=f"Findings ({len(findings)})")
    for col in ("sev", "category", "issue"):
        f_table.add_column(col, overflow="fold")
    for f in findings:
        f_table.add_row(f.severity.value, f.category, f.issue)
    console.print(f_table)

    return console.export_text(styles=True)
