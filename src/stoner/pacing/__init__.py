"""Pacing instrumentation: the book-level instrument layer.

Public API:

- :func:`run_pacing` -- run the deterministic instruments (and, unless
  ``llm=False``, the per-chapter advisory judge) over the whole manuscript,
  derive flatline runs, and save a :class:`~stoner.pacing.report.PacingReport`
  to ``.stoner/reviews/pacing-<ts>.{json,md}``.

The report is advisory only: no gate anywhere consumes it. See
`docs/plans/2026-07-07-004-feat-pacing-instrumentation-plan.md`.
"""

from __future__ import annotations

import time
from typing import Any

from ..ledger import Ledger
from ..project import WritingProject
from ..providers.base import Provider
from ..types import Finding, Usage
from .data import ChapterData, assemble_chapters
from .instruments import run_instruments
from .judge import ChapterJudgment, judge_chapters
from .report import PacingReport, derive_flatlines, flatline_finding, render

__all__ = ["run_pacing", "PacingReport", "render"]


def _series_row(
    chapter: ChapterData,
    instrument_series: dict[str, dict[int, Any]],
    judgment: ChapterJudgment | None,
) -> dict[str, Any]:
    seg = chapter.segments
    row: dict[str, Any] = {
        "chapter": chapter.number,
        "words": chapter.words,
        "dialogue_ratio": round(seg.dialogue_ratio, 3),
        "interiority_ratio": round(seg.interiority_ratio, 3),
        "action_ratio": round(seg.action_ratio, 3),
        "in_scene_fraction": round(seg.in_scene_fraction, 3),
        "pov": chapter.pov or "unknown",
        "ending_shape": instrument_series["ending_echo"].get(chapter.number, ""),
        "beat_sheet": instrument_series["beats"].get(chapter.number, "missing"),
    }
    if judgment is None:
        row["tension"] = "skipped"
        row["changes_hands"] = None
        row["beats"] = None
    else:
        row["tension"] = judgment.tension
        row["changes_hands"] = list(judgment.changes_hands)
        row["beats"] = {
            "landed": sum(1 for b in judgment.beats if b.verdict == "landed"),
            "drifted": sum(1 for b in judgment.beats if b.verdict == "drifted"),
            "missed": sum(1 for b in judgment.beats if b.verdict == "missed"),
        }
    return row


def run_pacing(
    project: WritingProject,
    llm: bool | None = None,
    model: str | None = None,
    provider: Provider | None = None,
    save: bool = True,
) -> PacingReport:
    """Run the full pacing instrument layer over the manuscript.

    `llm` defaults to `config.pacing.llm_instruments`; `llm=False` runs only
    the deterministic instruments (free, offline -- no provider is ever
    constructed). `provider`, if given, replaces the constructed provider
    (test injection). Raises `ValueError` when there are no chapters.
    """
    chapters = assemble_chapters(project)
    if not chapters:
        raise ValueError(
            "No chapters to analyze — draft some chapters first (stoner book)."
        )
    config = project.config.pacing
    use_llm = config.llm_instruments if llm is None else llm

    results = run_instruments(chapters, config)
    findings: list[Finding] = []
    for result in results.values():
        findings.extend(result.findings)

    judgments: list[ChapterJudgment] = []
    usage = Usage()
    model_str = ""
    if use_llm:
        model_str = model if model is not None else project.config.models.reviewer
        judgments, judge_findings, usage = judge_chapters(
            project, chapters, model=model, provider=provider
        )
        findings.extend(judge_findings)

    by_number = {j.chapter: j for j in judgments}
    instrument_series = {name: r.series for name, r in results.items()}
    series = [
        _series_row(ch, instrument_series, by_number.get(ch.number) if use_llm else None)
        for ch in chapters
    ]

    flatlines: list[tuple[int, int]] = []
    if use_llm:
        flatlines = derive_flatlines(judgments, config.flatline_min_run)
        findings.extend(flatline_finding(first, last) for first, last in flatlines)

    report = PacingReport(
        series=series,
        findings=findings,
        stats={name: r.stats for name, r in results.items()},
        flatlines=flatlines,
        llm=use_llm,
        model=model_str,
        usage=usage,
    )

    target = ""
    if save:
        ts = int(time.time())
        while (project.root / f".stoner/reviews/pacing-{ts}.json").exists():
            ts += 1  # never clobber a report saved in the same second
        base = f".stoner/reviews/pacing-{ts}"
        report.json_path = str(project.write(f"{base}.json", render(report, "json")))
        report.md_path = str(project.write(f"{base}.md", render(report, "markdown")))
        target = f"{base}.json"

    # every run ledgers, saved or not (hard invariant)
    Ledger(project.root).append(
        "pacing.report",
        target=target,
        chapters=len(chapters),
        findings=len(findings),
        flatline_runs=len(flatlines),
        llm=use_llm,
        saved=save,
    )
    return report
