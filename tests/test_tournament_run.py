"""Take drafting (U2) and blind pairwise judging (U3) tests.

No network anywhere: the provider is a scripted text-only fake whose judge
answers are derived from the presented take texts, so verdicts stay correct
under both presentation orders.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.ledger import Ledger
from stoner.project import WritingProject, split_frontmatter
from stoner.providers.base import Provider
from stoner.tournament.judge import run_judging
from stoner.tournament.state import (
    TakeRecord,
    TournamentState,
    load_state,
    save_state,
    takes_dir,
)
from stoner.tournament.takes import draft_takes, snapshot_chapter
from stoner.types import CompletionRequest, CompletionResponse, Usage

_PARAGRAPH = (
    "He walked to the window and stood there a while. The yard was bare. "
    "A dog crossed the road and stopped, looked back, and went on. He "
    "thought of his father's hands, how they had held the plow, and of the "
    "dry fields in August. Nothing in the house moved. "
)
LONG_PROSE = _PARAGRAPH * 8  # comfortably over the 200-word refusal guard


def make_draft(marker: str) -> str:
    return f"{LONG_PROSE}\n\n{marker}\n"


def text_response(text: str) -> CompletionResponse:
    return CompletionResponse(
        text=text, stop_reason="end", usage=Usage(input_tokens=10, output_tokens=20)
    )


class TournamentProvider(Provider):
    """Text-only scripted provider for tournament tests.

    Draft requests consume `drafts` in order. Judge requests (recognized by
    the '## Verdict' section) answer based on which side holds
    `winner_marker` -- correct under either presentation order -- unless a
    fixed `judge_response` (or a `judge_hook`) overrides that.
    """

    name = "scripted-tournament"
    supports_tools = False

    def __init__(
        self,
        drafts: list[str] | None = None,
        winner_marker: str = "",
        judge_response: str | None = None,
        judge_hook=None,
    ):
        self.drafts = list(drafts or [])
        self.winner_marker = winner_marker
        self.judge_response = judge_response
        self.judge_hook = judge_hook
        self.requests: list[CompletionRequest] = []
        self.judge_calls = 0

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        user = req.messages[0].content
        if "## Verdict" in user and "## Take A" in user:
            self.judge_calls += 1
            if self.judge_hook is not None:
                hook_result = self.judge_hook(self, user)
                if hook_result is not None:
                    return text_response(hook_result)
            if self.judge_response is not None:
                return text_response(self.judge_response)
            take_a = user.split("## Take A", 1)[1].split("## Take B", 1)[0]
            winner = "A" if self.winner_marker and self.winner_marker in take_a else "B"
            loser = "B" if winner == "A" else "A"
            return text_response(
                json.dumps(
                    {
                        "winner": winner,
                        "steal": {"from": loser, "move": f"keep the dog crossing ({loser})"},
                        "why": "stronger scene logic",
                    }
                )
            )
        return text_response(self.drafts.pop(0) if self.drafts else LONG_PROSE)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "testbook")
    scaffold_project(p, "testbook")
    return p


def seed_state(
    project: WritingProject, chapter: int = 1, angles: list[str] | None = None
) -> TournamentState:
    state = TournamentState(
        id=f"ch-{chapter:02d}-1111",
        chapter=chapter,
        planned_angles=angles or ["in_scene", "aftermath", "pov_tight"],
    )
    snapshot_chapter(project, state)
    save_state(project, state)
    return state


def write_take_file(
    project: WritingProject, state: TournamentState, index: int, angle: str, body: str
) -> None:
    """Synthetic captured take (U3 tests judge without drafting)."""
    rel = str(
        (takes_dir(project, state.id) / f"take-{index:02d}.md").relative_to(project.root)
    )
    project.write(rel, f"---\nangle: {angle}\n---\n\n{body}")
    state.takes.append(
        TakeRecord(index=index, angle=angle, rel_path=rel, words=len(body.split()))
    )


def ledger_actions(project: WritingProject, prefix: str = "tournament.") -> list[str]:
    return [
        e.action for e in Ledger(project.root).tail(500) if e.action.startswith(prefix)
    ]


# ---------------------------------------------------------------------------
# U2: take drafting
# ---------------------------------------------------------------------------


def test_draft_takes_captures_files_and_restores_chapter(project):
    project.write_chapter(1, {"title": "One", "status": "draft"}, "The original body.\n")
    before = project.read(project.chapter_rel(1))

    state = seed_state(project)
    provider = TournamentProvider(
        drafts=[make_draft("TAKE-ONE"), make_draft("TAKE-TWO"), make_draft("TAKE-THREE")]
    )
    draft_takes(project, state, provider=provider)

    assert len(state.takes) == 3
    for i, marker in ((1, "TAKE-ONE"), (2, "TAKE-TWO"), (3, "TAKE-THREE")):
        take_file = takes_dir(project, state.id) / f"take-{i:02d}.md"
        assert take_file.exists()
        fm, body = split_frontmatter(take_file.read_text(encoding="utf-8"))
        assert fm["angle"] == state.planned_angles[i - 1]
        assert fm["words"] > 200
        assert "slop" in fm
        assert marker in body

    # The manuscript chapter is exactly as it was before the tournament.
    assert project.read(project.chapter_rel(1)) == before
    assert ledger_actions(project).count("tournament.take") == 3


def test_draft_takes_absent_chapter_restored_to_stub(project):
    state = seed_state(project, chapter=2)
    assert state.chapter_existed is False
    provider = TournamentProvider(drafts=[make_draft("A"), make_draft("B"), make_draft("C")])
    draft_takes(project, state, provider=provider)

    # Chapter exists (never deleted) and is back to a scaffold stub.
    fm, body = project.read_chapter(2)
    assert fm.get("status") == "outline"
    assert "A dog crossed the road" not in body


def test_short_take_skipped_with_note_others_captured(project):
    project.write_chapter(1, {"title": "One", "status": "draft"}, "The original body.\n")
    state = seed_state(project)
    provider = TournamentProvider(
        drafts=[make_draft("TAKE-ONE"), "too short", make_draft("TAKE-THREE")]
    )
    draft_takes(project, state, provider=provider)

    assert [t.index for t in state.takes] == [1, 3]
    assert any("take 2" in n and "failed" in n for n in state.notes)
    assert not (takes_dir(project, state.id) / "take-02.md").exists()


def test_fewer_than_two_takes_raises(project):
    project.write_chapter(1, {"title": "One", "status": "draft"}, "The original body.\n")
    state = seed_state(project, angles=["in_scene", "aftermath"])
    provider = TournamentProvider(drafts=["nope", "nah"])
    with pytest.raises(RuntimeError, match="at least 2"):
        draft_takes(project, state, provider=provider)
    # The chapter survived regardless.
    assert "The original body." in project.read(project.chapter_rel(1))


def test_run_tournament_refuses_final_chapter_without_force(project):
    from stoner.tournament.run import run_tournament

    project.write_chapter(1, {"title": "One", "status": "final"}, "Done and dusted.\n")
    with pytest.raises(ValueError, match="--force"):
        run_tournament(project, 1, provider=TournamentProvider())

    # With force it proceeds (and still restores the chapter afterwards).
    provider = TournamentProvider(
        drafts=[make_draft("TAKE-ONE"), make_draft("TAKE-TWO")],
        winner_marker="TAKE-TWO",
    )
    res = run_tournament(project, 1, takes=2, provider=provider, force=True)
    assert res.status == "proposed"
    assert "Done and dusted." in project.read(project.chapter_rel(1))


# ---------------------------------------------------------------------------
# U3: blind pairwise judging
# ---------------------------------------------------------------------------


def judged_state(project, bodies: dict[int, str]) -> TournamentState:
    state = seed_state(project)
    state.planned_angles = ["in_scene", "aftermath", "pov_tight"][: len(bodies)]
    presets = ["in_scene", "aftermath", "pov_tight", "pov_distant"]
    for i, body in bodies.items():
        write_take_file(project, state, i, presets[(i - 1) % len(presets)], body)
    save_state(project, state)
    return state


def test_consistent_winner_proposed_with_ordered_ratings(project):
    state = judged_state(
        project,
        {1: make_draft("TAKE-ONE"), 2: make_draft("TAKE-TWO"), 3: make_draft("TAKE-THREE")},
    )
    provider = TournamentProvider(winner_marker="TAKE-TWO")
    run_judging(project, state, provider=provider)

    assert state.status == "proposed"
    assert state.proposed_winner == 2
    assert len(state.comparisons) == 3  # round-robin for n=3
    assert state.ratings[2] > state.ratings[1]
    assert state.ratings[2] > state.ratings[3]
    # Blindness: no judge prompt ever carried an angle name or a slop score.
    for req in provider.requests:
        assert "in_scene" not in req.messages[0].content
        assert "slop" not in req.messages[0].content.lower()
    actions = ledger_actions(project)
    assert actions.count("tournament.compare") == 3
    assert actions.count("tournament.propose") == 1


def test_order_disagreement_records_draw(project):
    state = judged_state(project, {1: make_draft("ONE"), 2: make_draft("TWO")})
    # A constant "A" verdict flips winners between presentation orders.
    provider = TournamentProvider(
        judge_response=json.dumps({"winner": "A", "steal": {"from": "B", "move": "m"}, "why": "w"})
    )
    run_judging(project, state, provider=provider)

    assert [c.verdict for c in state.comparisons] == ["draw"]
    assert "disagreed" in state.comparisons[0].note
    # Draw arithmetic between equal ratings: both unchanged.
    assert state.ratings[1] == pytest.approx(1200.0)
    assert state.ratings[2] == pytest.approx(1200.0)


def test_malformed_json_twice_records_draw_with_note(project):
    state = judged_state(project, {1: make_draft("ONE"), 2: make_draft("TWO")})
    provider = TournamentProvider(judge_response="i refuse to emit json")
    run_judging(project, state, provider=provider)

    assert state.status == "proposed"  # degradation never aborts the run
    assert [c.verdict for c in state.comparisons] == ["draw"]
    assert "unparseable" in state.comparisons[0].note
    # 2 orders x (1 try + 1 retry) = 4 judge calls
    assert provider.judge_calls == 4


def test_comparison_budget_stops_early_with_note_and_proposal(project):
    state = judged_state(
        project,
        {1: make_draft("TAKE-ONE"), 2: make_draft("TAKE-TWO"), 3: make_draft("TAKE-THREE")},
    )
    state.max_comparisons = 2  # one pair's worth of judge calls
    provider = TournamentProvider(winner_marker="TAKE-TWO")
    run_judging(project, state, provider=provider)

    assert len(state.comparisons) == 1
    assert state.comparisons_done == 2
    assert state.status == "proposed"
    assert state.proposed_winner is not None
    assert any("budget" in n for n in state.notes)


def test_state_saved_before_every_judge_call(project):
    state = judged_state(
        project,
        {1: make_draft("TAKE-ONE"), 2: make_draft("TAKE-TWO"), 3: make_draft("TAKE-THREE")},
    )

    observed: list[tuple[str, int]] = []

    def spy_hook(prov: TournamentProvider, user: str) -> None:
        on_disk = load_state(project, state.id)
        observed.append((on_disk.status, len(on_disk.comparisons)))
        return None  # fall through to marker-based verdict

    provider = TournamentProvider(winner_marker="TAKE-TWO", judge_hook=spy_hook)
    run_judging(project, state, provider=provider)

    # Every judge call found a state already saved as `judging`, with the
    # comparison log reflecting only fully-recorded pairs.
    assert observed
    assert all(status == "judging" for status, _ in observed)
    assert [n for _, n in observed] == [0, 0, 1, 1, 2, 2]


def test_steals_land_on_losing_takes(project):
    state = judged_state(
        project,
        {1: make_draft("TAKE-ONE"), 2: make_draft("TAKE-TWO"), 3: make_draft("TAKE-THREE")},
    )
    provider = TournamentProvider(winner_marker="TAKE-TWO")
    run_judging(project, state, provider=provider)

    winner = state.take(2)
    assert winner is not None and winner.steals == []
    for loser_idx in (1, 3):
        loser = state.take(loser_idx)
        assert loser is not None
        assert any("keep the dog" in s for s in loser.steals)
