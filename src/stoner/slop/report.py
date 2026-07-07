"""Rendering for :class:`~stoner.types.SlopReport` in three formats.

``render(report, fmt)`` supports ``"rich"`` (a table+summary, returned as
plain text captured from a `rich.console.Console`, and also suitable for
printing to a terminal), ``"markdown"``, and ``"json"``.
"""

from __future__ import annotations

import json

from ..types import SlopReport

#: Verdict bands, checked low-to-high; band applies while score < upper bound.
VERDICT_BANDS: list[tuple[float, str]] = [
    (15.0, "clean"),
    (30.0, "touched up"),
    (55.0, "slop-adjacent"),
    (float("inf"), "slop"),
]


def verdict(score: float) -> str:
    """Map a 0-100 score to a human verdict band."""
    for upper, label in VERDICT_BANDS:
        if score < upper:
            return label
    return VERDICT_BANDS[-1][1]


def _severity_order(sev: str) -> int:
    return {"critical": 0, "major": 1, "minor": 2, "info": 3}.get(sev, 4)


def render(report: SlopReport, fmt: str = "rich") -> str:
    """Render a SlopReport as ``"rich"``, ``"markdown"``, or ``"json"`` text."""
    if fmt == "json":
        return report.model_dump_json(indent=2)
    if fmt == "markdown":
        return _render_markdown(report)
    if fmt == "rich":
        return _render_rich(report)
    raise ValueError(f"unknown render format: {fmt!r} (expected rich|markdown|json)")


def _render_markdown(report: SlopReport) -> str:
    v = verdict(report.score)
    lines = [
        f"# Slop report: {report.path or '(unnamed)'}",
        "",
        f"**Score:** {report.score:.1f} / 100 -- **{v}**",
        "",
        "## Subscores",
        "",
        "| analyzer | subscore |",
        "|---|---|",
    ]
    for name, value in sorted(report.subscores.items()):
        lines.append(f"| {name} | {value:.1f} |")

    findings = sorted(report.findings, key=lambda f: _severity_order(f.severity.value))
    lines += ["", f"## Findings ({len(findings)})", ""]
    if not findings:
        lines.append("_No findings._")
    for f in findings:
        loc = f"L{f.span.line}" if f.span else "-"
        quote = f" -- `{f.quote}`" if f.quote else ""
        lines.append(f"- **[{f.severity.value}]** ({f.category}) {loc}: {f.issue}{quote}")

    lines += ["", "## Stats", "", "```json", json.dumps(report.stats, indent=2, default=str), "```"]
    return "\n".join(lines)


def _render_rich(report: SlopReport) -> str:
    from rich.console import Console
    from rich.table import Table

    console = Console(record=True, width=100)
    v = verdict(report.score)
    console.print(f"[bold]Slop report[/bold]: {report.path or '(unnamed)'}")
    console.print(f"Score: [bold]{report.score:.1f}[/bold] / 100 -- verdict: [bold]{v}[/bold]")

    sub_table = Table(title="Subscores")
    sub_table.add_column("Analyzer")
    sub_table.add_column("Subscore", justify="right")
    for name, value in sorted(report.subscores.items()):
        sub_table.add_row(name, f"{value:.1f}")
    console.print(sub_table)

    findings = sorted(report.findings, key=lambda f: _severity_order(f.severity.value))
    f_table = Table(title=f"Findings ({len(findings)})")
    f_table.add_column("Sev")
    f_table.add_column("Category")
    f_table.add_column("Line", justify="right")
    f_table.add_column("Issue")
    for f in findings:
        loc = str(f.span.line) if f.span else "-"
        f_table.add_row(f.severity.value, f.category, loc, f.issue)
    console.print(f_table)

    return console.export_text()
