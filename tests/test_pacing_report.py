"""Tests for run_pacing / PacingReport (U4): aggregation, flatline
derivation, rendering, and saving. Offline via FakeProvider."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.ledger import Ledger
from stoner.pacing import run_pacing
from stoner.pacing.judge import ChapterJudgment
from stoner.pacing.report import derive_flatlines, render
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.types import CompletionRequest, CompletionResponse, Usage

SCENE_BODY = (
    '"We hold the bridge," Mara said, planting the standard in the mud.\n\n'
    '"With what?" Holt asked. "Half the company is barefoot and the other '
    'half is asleep on its feet."\n\n'
    'She took the ledger from his hands and threw it in the river.\n'
)
SUMMARY_BODY = (
    "The company had marched for six days. By the time they had reached the "
    "bridge they had eaten the last of the salt pork, and over the next two "
    "days they had traded most of their powder for bread.\n\n"
    "Holt had argued for turning back. Mara had refused, and the argument "
    "had settled into silence by the time the rains had come.\n"
)


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, responses: list | Callable[[int], CompletionResponse]):
        self.responses = responses
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        if callable(self.responses):
            item = self.responses(idx)
        else:
            item = self.responses[min(idx, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _resp(tension: str, changes: list[str] | None = None) -> CompletionResponse:
    payload = {"tension": tension, "tension_why": "x", "changes_hands": changes or [], "beats": []}
    return CompletionResponse(
        text="```json\n" + json.dumps(payload) + "\n```",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Report Book")
    scaffold_project(proj, "Report Book")
    for n in range(1, 5):
        body = SCENE_BODY if n == 1 else SUMMARY_BODY
        proj.write_chapter(n, {"title": f"Ch{n}", "pov": "Mara"}, body)
    return proj


# -- derive_flatlines (pure) --------------------------------------------------


def _j(chapter: int, tension: str, changes: list[str] | None = None) -> ChapterJudgment:
    return ChapterJudgment(chapter=chapter, tension=tension, changes_hands=changes or [])


def test_derive_flatlines_finds_holds_sags_with_empty_ledgers():
    judgments = [_j(1, "opens"), _j(2, "holds"), _j(3, "sags"), _j(4, "holds")]
    assert derive_flatlines(judgments, min_run=3) == [(2, 4)]


def test_derive_flatlines_rises_in_middle_breaks_run():
    judgments = [_j(1, "opens"), _j(2, "holds"), _j(3, "rises"), _j(4, "holds")]
    assert derive_flatlines(judgments, min_run=3) == []


def test_derive_flatlines_changes_hands_breaks_run():
    judgments = [_j(1, "opens"), _j(2, "holds"), _j(3, "holds", ["the map changes hands"]), _j(4, "holds")]
    assert derive_flatlines(judgments, min_run=3) == []


def test_derive_flatlines_run_at_end_is_captured():
    judgments = [_j(n, "sags") for n in range(1, 4)]
    assert derive_flatlines(judgments, min_run=3) == [(1, 3)]


# -- run_pacing -----------------------------------------------------------------


def test_full_run_saves_both_files_and_ledgers(project: WritingProject):
    provider = FakeProvider(lambda i: _resp("opens" if i == 0 else "rises", ["something moves"]))
    report = run_pacing(project, llm=True, provider=provider)

    assert len(report.series) == 4
    assert report.llm is True
    assert Path(report.json_path).exists()
    assert Path(report.md_path).exists()

    payload = json.loads(Path(report.json_path).read_text(encoding="utf-8"))
    assert payload["kind"] == "pacing"
    assert len(payload["series"]) == 4
    assert payload["series"][0]["tension"] == "opens"

    entries = Ledger(project.root).tail(20)
    report_entries = [e for e in entries if e.action == "pacing.report"]
    judge_entries = [e for e in entries if e.action == "pacing.judge"]
    assert len(report_entries) == 1
    assert report_entries[0].detail["chapters"] == 4
    assert len(judge_entries) == 4


def test_no_llm_run_never_constructs_provider(project: WritingProject, monkeypatch):
    import stoner.pipelines.common as common

    def boom(*args, **kwargs):
        raise AssertionError("provider must not be constructed for --no-llm")

    monkeypatch.setattr(common, "get_provider", boom)
    report = run_pacing(project, llm=False)

    assert report.llm is False
    assert all(row["tension"] == "skipped" for row in report.series)
    assert all(row["changes_hands"] is None for row in report.series)
    # deterministic findings still present (summary-heavy chapters 2-4)
    assert any(f.source == "pacing:scene_map" for f in report.findings)
    assert report.flatlines == []
    entries = Ledger(project.root).tail(20)
    assert [e.action for e in entries if e.action.startswith("pacing.")] == ["pacing.report"]


def test_flatline_finding_uses_target_diagnostic_phrasing(project: WritingProject):
    responses = [_resp("opens"), _resp("holds"), _resp("sags"), _resp("holds")]
    report = run_pacing(project, llm=True, provider=FakeProvider(responses))

    assert report.flatlines == [(2, 4)]
    flatline = [f for f in report.findings if f.source == "pacing:flatline"]
    assert len(flatline) == 1
    assert flatline[0].severity.value == "major"
    assert flatline[0].issue == "Chapters 2–4 flatline; nothing changes hands."
    # the diagnostic sentence renders verbatim in markdown
    assert "Chapters 2–4 flatline; nothing changes hands." in render(report, "markdown")


def test_llm_defaults_to_config_flag(project: WritingProject):
    project.config.pacing.llm_instruments = False
    report = run_pacing(project)  # no provider available: must not need one
    assert report.llm is False


def test_render_all_formats_and_unknown_raises(project: WritingProject, capsys):
    report = run_pacing(project, llm=False, save=False)
    for fmt in ("rich", "markdown", "json"):
        assert render(report, fmt)
    json.loads(render(report, "json"))  # valid JSON
    assert capsys.readouterr().out == ""  # render is pure: nothing printed
    with pytest.raises(ValueError):
        render(report, "html")


def test_no_save_writes_no_files_but_still_ledgers(project: WritingProject):
    report = run_pacing(project, llm=False, save=False)
    assert report.json_path == "" and report.md_path == ""
    assert list((project.root / ".stoner" / "reviews").glob("pacing-*")) == []
    # every run ledgers, saved or not
    entries = [e for e in Ledger(project.root).tail(20) if e.action == "pacing.report"]
    assert len(entries) == 1
    assert entries[0].detail["saved"] is False
    assert entries[0].target == ""


def test_zero_chapter_project_raises_value_error(tmp_path: Path):
    proj = WritingProject.create(tmp_path / "empty", "Empty")
    scaffold_project(proj, "Empty")
    with pytest.raises(ValueError, match="draft some chapters first"):
        run_pacing(proj, llm=False)


def test_series_rows_carry_every_timeline_column(project: WritingProject):
    report = run_pacing(project, llm=False, save=False)
    row = report.series[0]
    for key in (
        "chapter", "words", "dialogue_ratio", "interiority_ratio", "action_ratio",
        "in_scene_fraction", "pov", "ending_shape", "beat_sheet", "tension",
        "changes_hands", "beats",
    ):
        assert key in row
