"""Autonomous book-mode tests. No network: providers are scripted fakes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from stoner.canon.memory import Memory
from stoner.canon.scaffold import new_beats_stub, scaffold_project
from stoner.pipelines import book as book_mod
from stoner.pipelines.book import (
    BookResult,
    BookState,
    ChapterDone,
    load_state,
    planned_chapters,
    run_book,
    save_state,
    state_path,
)
from stoner.pipelines.write import WriteResult
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.review.book_review import BookReviewReport, run_book_review
from stoner.types import CompletionRequest, CompletionResponse, Finding, Severity, Usage

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

_CLEAN_PARAGRAPHS = [
    "He walked to the window and stood there a while. The yard was bare. A dog crossed the road and stopped, looked back, and went on.",
    "He thought of his father's hands, how they had held the plow, and of the dry fields in August. Nothing in the house moved.",
    "He had lived here forty years and knew each sound the boards made, and none came now. The kettle sat cold on the stove.",
    "In the morning there would be work: the fence along the north line, the gate that sagged, a letter he owed his brother.",
    "She had left the garden in October, and the beds still held their shapes under the frost, patient as graves.",
    "Once, when he was a boy, his mother had read to him at this table by lamplight, her voice low against the wind outside.",
    "The clock in the hall kept its slow count. He did not wind it anymore, yet somehow it went on, losing a minute a day.",
    "A truck passed on the county road, its lights sweeping the ceiling, and the room returned to dark behind it.",
    "He ate standing up, bread and the end of the ham, and washed the plate and set it in the rack where hers used to go.",
    "Later he took down the ledger and entered the day's figures in his careful hand: feed, diesel, the vet's bill from spring.",
    "Sleep came the way it always came now, without announcement, somewhere between one worry and the next.",
    "Before dawn the birds began, first one and then the whole hedge, and he lay listening as the window went gray.",
]
CLEAN_PROSE = "\n\n".join(_CLEAN_PARAGRAPHS)

ARCHIVIST_JSON = """{
  "summary": "William reflects at the window; the farm is failing.",
  "facts": [
    {"entity": "William", "kind": "character", "field": "home", "value": "the farm", "quote": "He had lived here forty years"}
  ],
  "new_entities": [],
  "thread_updates": []
}"""

BOOK_MAJOR_JSON = json.dumps(
    {
        "findings": [
            {
                "chapter": 2,
                "severity": "major",
                "category": "pacing",
                "quote": "",
                "issue": "Chapter 2 sags in the middle.",
                "suggestion": "Convert summary to scene.",
            }
        ],
        "overall": "Solid, but chapter two drags.",
        "verdict": "needs-work",
    }
)
BOOK_CLEAN_JSON = json.dumps({"findings": [], "overall": "Reads well.", "verdict": "ready"})


def _resp(text: str) -> CompletionResponse:
    return CompletionResponse(text=text, stop_reason="end", usage=Usage(input_tokens=10, output_tokens=20))


class RoutingProvider(Provider):
    """Routes each completion by inspecting the request's system prompt:
    book-review -> book JSON (major first, then clean), archivist -> facts
    JSON, everything else (writer draft) -> clean prose."""

    name = "routing"
    supports_tools = True

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []
        self.book_calls = 0

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        sys = (req.system or "").lower()
        if "professor of fiction" in sys:
            self.book_calls += 1
            return _resp(BOOK_MAJOR_JSON if self.book_calls == 1 else BOOK_CLEAN_JSON)
        if "archivist for" in sys:
            return _resp(ARCHIVIST_JSON)
        return _resp(CLEAN_PROSE)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "testbook")
    scaffold_project(p, "testbook")
    return p


def _add_beats(project: WritingProject, *numbers: int) -> None:
    for n in numbers:
        project.write(f"outline/beats/ch-{n:02d}.md", new_beats_stub(n))


# ---------------------------------------------------------------------------
# BookState: save / load / corruption
# ---------------------------------------------------------------------------


def test_state_save_load_roundtrip(project):
    state = BookState(
        chapters_planned=[1, 2, 3],
        chapters_done={2: ChapterDone(words=1200, slop=8.0, reviewed=True, revision_cycles=1)},
        current=3,
        phase="reviewing",
    )
    save_state(project, state)
    loaded = load_state(project)
    assert loaded.chapters_planned == [1, 2, 3]
    assert loaded.current == 3
    assert loaded.phase == "reviewing"
    # int keys survive the JSON round-trip
    assert loaded.chapters_done[2].words == 1200
    assert loaded.chapters_done[2].reviewed is True
    assert loaded.updated_at >= state.started_at


def test_load_missing_state_is_fresh(project):
    assert not state_path(project).exists()
    state = load_state(project)
    assert state.chapters_planned == []
    assert state.phase == "drafting"


def test_corrupt_state_backed_up_and_fresh(project):
    p = state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json at all", encoding="utf-8")
    state = load_state(project)
    # treated as fresh
    assert state.chapters_planned == []
    assert state.phase == "drafting"
    # the bad file was preserved as a .bak
    backup = p.parent / (p.name + ".bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "{not valid json at all"


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def test_planned_chapters_from_beats_and_existing(project):
    _add_beats(project, 2, 3)  # ch-01 beats ship with the scaffold
    project.write_chapter(5, {"title": "Five"}, CLEAN_PROSE)
    assert planned_chapters(project) == [1, 2, 3, 5]


def test_run_book_refuses_without_beats(tmp_path):
    bare = WritingProject.create(tmp_path / "bare", "bare")  # no scaffold, no beats
    with pytest.raises(ValueError, match="No chapters to write"):
        run_book(bare, provider=RoutingProvider())


# ---------------------------------------------------------------------------
# run_book: full loop (real run_write + run_book_review, revise observed)
# ---------------------------------------------------------------------------


def test_run_book_full_loop(project, monkeypatch):
    _add_beats(project, 2, 3)
    revise_calls: list[int] = []

    def fake_revise(project_, chapter, findings, model=None, provider=None):
        revise_calls.append(chapter)

        @dataclass
        class R:
            usage: Usage = field(default_factory=Usage)

        return R()

    monkeypatch.setattr("stoner.review.revise.revise_chapter", fake_revise)

    events: list[dict] = []
    provider = RoutingProvider()
    res = run_book(project, provider=provider, review_every=4, max_review_rounds=2,
                   on_event=events.append)

    # three chapters drafted
    assert res.chapters_written == 3
    for n in (1, 2, 3):
        _, body = project.read_chapter(n)
        assert "He walked to the window" in body

    # end-of-run review fired twice (major -> revise -> clean re-review)
    assert res.review_rounds == 2
    assert revise_calls == [2]
    assert res.remaining_major_findings == 0
    assert res.remaining_critical_findings == 0

    # state progressed and is marked done
    state = load_state(project)
    assert state.phase == "done"
    assert set(state.chapters_done) == {1, 2, 3}
    assert state.chapters_done[2].reviewed is True
    assert state.chapters_done[2].revision_cycles >= 1
    assert len(state.book_reviews) == 2

    # progress events surfaced
    assert sum(1 for e in events if e["type"] == "chapter.done") == 3
    assert any(e["type"] == "review.round" for e in events)


# ---------------------------------------------------------------------------
# resume / max_chapters (run_write + run_book_review isolated)
# ---------------------------------------------------------------------------


def _install_fake_write(monkeypatch, counter: list[int]):
    def fake_write(project_, number, model=None, provider=None, **kw):
        counter.append(number)
        project_.write_chapter(number, {"title": f"C{number}", "status": "draft"}, CLEAN_PROSE * 3)
        _, body = project_.read_chapter(number)
        from stoner.project import count_words

        return WriteResult(chapter=number, words=count_words(body), slop_after=5.0,
                           gate_passed=True, revision_loops=0, usage=Usage())

    monkeypatch.setattr(book_mod, "run_write", fake_write)


def _install_clean_review(monkeypatch):
    def fake_review(project_, model=None, provider=None):
        return BookReviewReport(findings=[], overall="clean", verdict="ready")

    monkeypatch.setattr(book_mod, "run_book_review", fake_review)


def test_resume_skips_completed_chapters(project, monkeypatch):
    _add_beats(project, 2, 3)
    calls: list[int] = []
    _install_fake_write(monkeypatch, calls)
    _install_clean_review(monkeypatch)

    first = run_book(project, provider=RoutingProvider())
    assert first.chapters_written == 3
    assert calls == [1, 2, 3]

    calls.clear()
    second = run_book(project, provider=RoutingProvider(), resume=True)
    assert second.chapters_written == 0
    assert calls == []  # nothing re-drafted


def test_max_chapters_honored(project, monkeypatch):
    _add_beats(project, 2, 3, 4, 5)
    calls: list[int] = []
    _install_fake_write(monkeypatch, calls)
    _install_clean_review(monkeypatch)

    res = run_book(project, provider=RoutingProvider(), max_chapters=2)
    assert res.chapters_written == 2
    assert calls == [1, 2]
    state = load_state(project)
    assert state.budget.max_chapters_per_run == 2


def test_plateau_stops_review_rounds_early(project, monkeypatch):
    _add_beats(project, 2)
    _install_fake_write(monkeypatch, [])

    # every review returns the same two majors -> no improvement -> plateau
    def fake_review(project_, model=None, provider=None):
        return BookReviewReport(
            findings=[
                Finding(source="review:book", severity=Severity.major, category="ch-01:x", issue="a"),
                Finding(source="review:book", severity=Severity.major, category="ch-02:y", issue="b"),
            ],
            overall="stuck",
            verdict="needs-work",
        )

    monkeypatch.setattr(book_mod, "run_book_review", fake_review)
    revise_calls: list[int] = []

    def fake_revise(project_, chapter, findings, model=None, provider=None):
        revise_calls.append(chapter)

        @dataclass
        class R:
            usage: Usage = field(default_factory=Usage)

        return R()

    monkeypatch.setattr("stoner.review.revise.revise_chapter", fake_revise)

    res = run_book(project, provider=RoutingProvider(), max_review_rounds=5)
    # round 1 majors=2 -> revise; round 2 majors=2 (>= prev) -> plateau stop
    assert res.review_rounds == 2
    assert res.remaining_major_findings == 2
    assert sorted(revise_calls) == [1, 2]  # revised once each in round 1 only


# ---------------------------------------------------------------------------
# run_book_review: assembly picks full-text vs summaries
# ---------------------------------------------------------------------------


def test_book_review_short_book_full_text(project):
    for n in (1, 2):
        project.write_chapter(n, {"title": f"C{n}"}, f"SENTINEL_{n}_UNIQUE\n\n" + CLEAN_PROSE)

    class Capture(Provider):
        name = "cap"
        supports_tools = True

        def __init__(self):
            self.reqs = []

        def complete(self, req):
            self.reqs.append(req)
            return _resp(BOOK_CLEAN_JSON)

    cap = Capture()
    report = run_book_review(project, provider=cap)
    prompt = cap.reqs[0].messages[0].content
    assert "SENTINEL_1_UNIQUE" in prompt and "SENTINEL_2_UNIQUE" in prompt
    assert report.chapters_full == [1, 2]
    assert report.chapters_summarized == []


def test_book_review_long_book_summarizes_old_chapters(project):
    mem = Memory(project)
    for n in range(1, 15):  # 14 chapters
        project.write_chapter(n, {"title": f"C{n}"}, f"SENTINEL_{n}_UNIQUE\n\n" + ("word " * 40))
        mem.set_chapter_summary(n, f"SUMMARY_{n}_TEXT")

    class Capture(Provider):
        name = "cap"
        supports_tools = True

        def __init__(self):
            self.reqs = []

        def complete(self, req):
            self.reqs.append(req)
            return _resp(BOOK_CLEAN_JSON)

    cap = Capture()
    report = run_book_review(project, provider=cap)
    prompt = cap.reqs[0].messages[0].content

    # latest 6 (9..14) full text; earlier ones summary-only
    assert report.chapters_full == [9, 10, 11, 12, 13, 14]
    assert report.chapters_summarized == list(range(1, 9))
    for n in range(9, 15):
        assert f"SENTINEL_{n}_UNIQUE" in prompt
    for n in range(1, 9):
        assert f"SENTINEL_{n}_UNIQUE" not in prompt
        assert f"SUMMARY_{n}_TEXT" in prompt


def test_book_review_maps_findings_to_chapters_and_locates_quote(project):
    quote = "He had lived here forty years and knew each sound the boards made"
    project.write_chapter(1, {"title": "One"}, CLEAN_PROSE)
    project.write_chapter(2, {"title": "Two"}, CLEAN_PROSE)
    payload = json.dumps(
        {
            "findings": [
                {"chapter": 2, "severity": "critical", "category": "plot",
                 "quote": quote, "issue": "hole", "suggestion": "fix"},
                {"chapter": 1, "severity": "minor", "category": "polish",
                 "quote": "", "issue": "nit", "suggestion": "trim"},
            ],
            "overall": "mixed",
            "verdict": "needs-work",
        }
    )

    class One(Provider):
        name = "one"
        supports_tools = True

        def complete(self, req):
            return _resp(payload)

    report = run_book_review(project, provider=One())
    assert report.major_count == 1  # the critical one
    assert report.critical_count == 1
    grouped = report.by_chapter()
    assert set(grouped) == {1, 2}
    crit = grouped[2][0]
    assert crit.severity is Severity.critical
    assert crit.span is not None  # quote located in ch-2 body
    # report saved to disk
    assert Path(report.json_path).exists()
    assert Path(report.md_path).exists()


# ---------------------------------------------------------------------------
# CLI smoke (pipeline functions monkeypatched)
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_app():
    from stoner.cli.book_cmds import register

    app = typer.Typer()
    register(app)
    return app


def test_cli_book_smoke(project, monkeypatch, cli_app):
    monkeypatch.chdir(project.root)

    def fake_run_book(project_, **kw):
        return BookResult(chapters_written=3, total_words=3600, review_rounds=1,
                          remaining_major_findings=0, remaining_critical_findings=0,
                          state_path=str(state_path(project_)))

    monkeypatch.setattr(book_mod, "run_book", fake_run_book)
    result = CliRunner().invoke(cli_app, ["book", "--no-resume"])
    assert result.exit_code == 0, result.output
    assert "chapters written" in result.output
    assert "3" in result.output


def test_cli_book_nonzero_on_critical(project, monkeypatch, cli_app):
    monkeypatch.chdir(project.root)

    def fake_run_book(project_, **kw):
        return BookResult(chapters_written=1, remaining_critical_findings=2,
                          state_path=str(state_path(project_)))

    monkeypatch.setattr(book_mod, "run_book", fake_run_book)
    result = CliRunner().invoke(cli_app, ["book"])
    assert result.exit_code == 1


def test_cli_review_book_smoke(project, monkeypatch, cli_app):
    monkeypatch.chdir(project.root)

    def fake_review(project_, model=None):
        return BookReviewReport(
            findings=[Finding(source="review:book", severity=Severity.major,
                              category="ch-02:pacing", issue="drags")],
            overall="ok", verdict="needs-work", json_path="/x.json",
        )

    monkeypatch.setattr("stoner.review.book_review.run_book_review", fake_review)
    result = CliRunner().invoke(cli_app, ["review-book"])
    assert result.exit_code == 0, result.output
    assert "verdict" in result.output
    assert "chapter 2" in result.output.lower()
