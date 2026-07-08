"""Rendering + saving for the deterministic motif reports.

`render(report, fmt)` is pure (a detached recording `rich.Console`, returns
text) with the same three formats as `slop/report.py`. Every saved JSON
payload carries a top-level ``kind: "motifs"`` -- the repo-wide report
discriminator that `.stoner/reviews/` routing keys on -- and reports save to
``.stoner/reviews/motif-scan-<ts>.json`` / ``motif-rhyme-<ts>.json``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..project import WritingProject
from .scan import CandidateReport, MotifScanReport, RhymeReport

_KIND = "motifs"


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def scan_payload(report: MotifScanReport) -> dict[str, Any]:
    return {
        "kind": _KIND,
        "report": "scan",
        "created_at": report.created_at,
        "chapters": report.chapters,
        "rows": [
            {
                "motif_id": r.motif_id,
                "motif": r.motif,
                "per_chapter": {str(n): c for n, c in sorted(r.per_chapter.items())},
                "chapters_hit": r.chapters_hit,
                "first": r.first,
                "last": r.last,
            }
            for r in report.rows
        ],
    }


def candidate_payload(report: CandidateReport) -> dict[str, Any]:
    return {
        "kind": _KIND,
        "report": "candidates",
        "created_at": report.created_at,
        "min_chapters": report.min_chapters,
        "cap": report.cap,
        "candidates": [
            {"gram": c.gram, "chapters": c.chapters, "total": c.total}
            for c in report.candidates
        ],
    }


def rhyme_payload(report: RhymeReport) -> dict[str, Any]:
    return {
        "kind": _KIND,
        "report": "rhyme",
        "created_at": report.created_at,
        "jaccard": report.jaccard,
        "shared_distinctive": report.shared_distinctive,
        "motifs_both": report.motifs_both,
        "motifs_open_only": report.motifs_open_only,
        "motifs_close_only": report.motifs_close_only,
        "opening_chapters": report.opening_chapters,
        "closing_chapters": report.closing_chapters,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(report: MotifScanReport | CandidateReport | RhymeReport, fmt: str = "rich") -> str:
    """Render a motif report as ``"rich"``, ``"markdown"``, or ``"json"``."""
    if fmt not in ("rich", "markdown", "json"):
        raise ValueError(f"unknown render format: {fmt!r} (expected rich|markdown|json)")
    if isinstance(report, MotifScanReport):
        if fmt == "json":
            return json.dumps(scan_payload(report), indent=2, ensure_ascii=False)
        return _scan_markdown(report) if fmt == "markdown" else _scan_rich(report)
    if isinstance(report, CandidateReport):
        if fmt == "json":
            return json.dumps(candidate_payload(report), indent=2, ensure_ascii=False)
        return _candidate_markdown(report) if fmt == "markdown" else _candidate_rich(report)
    if fmt == "json":
        return json.dumps(rhyme_payload(report), indent=2, ensure_ascii=False)
    return _rhyme_markdown(report) if fmt == "markdown" else _rhyme_rich(report)


def _detached_console() -> Any:
    import io

    from rich.console import Console

    return Console(record=True, width=100, file=io.StringIO(), force_terminal=True)


# -- scan --------------------------------------------------------------------


def _scan_markdown(report: MotifScanReport) -> str:
    lines = ["# Motif recurrence", "", f"Chapters scanned: {len(report.chapters)}", ""]
    if not report.rows:
        lines.append("_No motifs registered. Add one with `stoner motifs add`._")
        return "\n".join(lines)
    header = ["motif", *[f"ch{n:02d}" for n in report.chapters], "hits"]
    lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in report.rows:
        cells = [r.motif] + [str(r.per_chapter.get(n, 0)) for n in report.chapters] + [str(r.chapters_hit)]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _scan_rich(report: MotifScanReport) -> str:
    from rich.table import Table

    console = _detached_console()
    console.print("[bold]Motif recurrence[/bold]")
    if not report.rows:
        console.print("[dim]no motifs registered — add one with `stoner motifs add`[/dim]")
        return console.export_text(styles=True)
    table = Table(title=f"across {len(report.chapters)} chapter(s)")
    table.add_column("motif")
    for n in report.chapters:
        table.add_column(f"{n:02d}", justify="right")
    table.add_column("hits", justify="right")
    for r in report.rows:
        cells = [r.motif] + [str(r.per_chapter.get(n, 0)) for n in report.chapters] + [str(r.chapters_hit)]
        table.add_row(*cells)
    console.print(table)
    return console.export_text(styles=True)


# -- candidates --------------------------------------------------------------


def _candidate_markdown(report: CandidateReport) -> str:
    lines = [
        "# Motif candidates",
        "",
        f"Unregistered n-grams recurring across >= {report.min_chapters} chapters.",
        "",
    ]
    if not report.candidates:
        lines.append("_No candidates._")
        return "\n".join(lines)
    lines += ["| gram | chapters | total |", "|---|---|---|"]
    for c in report.candidates:
        lines.append(f"| {c.gram} | {', '.join(str(n) for n in c.chapters)} | {c.total} |")
    return "\n".join(lines)


def _candidate_rich(report: CandidateReport) -> str:
    from rich.table import Table

    console = _detached_console()
    console.print("[bold]Motif candidates[/bold]")
    if not report.candidates:
        console.print("[dim]no candidates found[/dim]")
        return console.export_text(styles=True)
    table = Table(title=f">= {report.min_chapters} chapters")
    for col in ("gram", "chapters", "total"):
        table.add_column(col)
    for c in report.candidates:
        table.add_row(c.gram, ", ".join(str(n) for n in c.chapters), str(c.total))
    console.print(table)
    return console.export_text(styles=True)


# -- rhyme -------------------------------------------------------------------


def _rhyme_lines(report: RhymeReport) -> list[str]:
    return [
        f"opening chapters: {report.opening_chapters}",
        f"closing chapters: {report.closing_chapters}",
        f"content-token Jaccard: {report.jaccard:.4f}",
        f"shared distinctive terms: {', '.join(report.shared_distinctive) or '-'}",
        f"motifs in both: {', '.join(report.motifs_both) or '-'}",
        f"motifs opening-only: {', '.join(report.motifs_open_only) or '-'}",
        f"motifs closing-only: {', '.join(report.motifs_close_only) or '-'}",
    ]


def _rhyme_markdown(report: RhymeReport) -> str:
    return "# Ending rhymes with opening?\n\n" + "\n".join(
        f"- {line}" for line in _rhyme_lines(report)
    )


def _rhyme_rich(report: RhymeReport) -> str:
    console = _detached_console()
    console.print("[bold]Ending rhymes with opening?[/bold]")
    for line in _rhyme_lines(report):
        console.print(line)
    return console.export_text(styles=True)


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def _save(project: WritingProject, stem: str, payload: dict[str, Any]) -> str:
    ts = int(time.time())
    while (project.root / f".stoner/reviews/{stem}-{ts}.json").exists():
        ts += 1  # never clobber a report saved in the same second
    rel = f".stoner/reviews/{stem}-{ts}.json"
    project.write(rel, json.dumps(payload, indent=2, ensure_ascii=False))
    return rel


def save_scan(project: WritingProject, report: MotifScanReport) -> str:
    rel = _save(project, "motif-scan", scan_payload(report))
    report.json_path = rel
    return rel


def save_rhyme(project: WritingProject, report: RhymeReport) -> str:
    rel = _save(project, "motif-rhyme", rhyme_payload(report))
    report.json_path = rel
    return rel
