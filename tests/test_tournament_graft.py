"""Orchestrator, resume, apply, and graft tests (U4). No network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.ledger import Ledger
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.tournament.run import apply_winner, run_tournament
from stoner.tournament.state import load_state
from stoner.types import CompletionRequest, CompletionResponse, Usage

_PARAGRAPH = (
    "He walked to the window and stood there a while. The yard was bare. "
    "A dog crossed the road and stopped, looked back, and went on. He "
    "thought of his father's hands, how they had held the plow, and of the "
    "dry fields in August. Nothing in the house moved. "
)
LONG_PROSE = _PARAGRAPH * 8
GRAFTED_PROSE = ("The kettle sat cold on the stove and he counted the hours. " * 60) + "GRAFTED"


def make_draft(marker: str) -> str:
    return f"{LONG_PROSE}\n\n{marker}\n"


def text_response(text: str) -> CompletionResponse:
    return CompletionResponse(
        text=text, stop_reason="end", usage=Usage(input_tokens=10, output_tokens=20)
    )


class TournamentProvider(Provider):
    """Text-only scripted provider covering draft, judge, and graft calls."""

    name = "scripted-tournament"
    supports_tools = False

    def __init__(
        self,
        drafts: list[str] | None = None,
        winner_marker: str = "",
        graft_response: str | None = None,
        judge_hook=None,
    ):
        self.drafts = list(drafts or [])
        self.winner_marker = winner_marker
        self.graft_response = graft_response
        self.judge_hook = judge_hook
        self.requests: list[CompletionRequest] = []
        self.judge_calls = 0
        self.draft_calls = 0
        self.graft_calls = 0

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        user = req.messages[0].content
        if "## Verdict" in user and "## Take A" in user:
            self.judge_calls += 1
            if self.judge_hook is not None:
                self.judge_hook(self)
            take_a = user.split("## Take A", 1)[1].split("## Take B", 1)[0]
            winner = "A" if self.winner_marker and self.winner_marker in take_a else "B"
            loser = "B" if winner == "A" else "A"
            return text_response(
                json.dumps(
                    {
                        "winner": winner,
                        "steal": {"from": loser, "move": f"keep the dog crossing ({loser})"},
                        "why": "stronger",
                    }
                )
            )
        if "## Steals to graft" in user:
            self.graft_calls += 1
            body = self.graft_response if self.graft_response is not None else GRAFTED_PROSE
            return text_response(f"SUMMARY: folded steals in\nBEGIN CHAPTER\n{body}\nEND CHAPTER")
        self.draft_calls += 1
        return text_response(self.drafts.pop(0) if self.drafts else LONG_PROSE)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "testbook")
    scaffold_project(p, "testbook")
    p.write_chapter(1, {"title": "One", "status": "draft"}, "The original body.\n")
    return p


def full_provider() -> TournamentProvider:
    return TournamentProvider(
        drafts=[make_draft("TAKE-ONE"), make_draft("TAKE-TWO"), make_draft("TAKE-THREE")],
        winner_marker="TAKE-TWO",
    )


def tournament_actions(project: WritingProject) -> list[str]:
    return [
        e.action
        for e in Ledger(project.root).tail(500)
        if e.action.startswith("tournament.")
    ]


# ---------------------------------------------------------------------------
# run_tournament end to end
# ---------------------------------------------------------------------------


def test_full_run_proposes_winner_and_leaves_manuscript_alone(project):
    before = project.read(project.chapter_rel(1))
    res = run_tournament(project, 1, takes=3, provider=full_provider())

    assert res.takes == 3
    assert res.comparisons == 3
    assert res.proposed_winner == 2
    assert res.status == "proposed"
    assert res.usage.output_tokens > 0
    # Nothing wrote to the manuscript: proposal is the autonomy ceiling.
    assert project.read(project.chapter_rel(1)) == before

    on_disk = load_state(project, res.id)
    assert on_disk.status == "proposed"
    assert on_disk.proposed_winner == 2


def test_resume_after_crash_mid_judging_completes_without_redrafting(project):
    calls = {"n": 0}

    def crash_on_third_judge_call(prov: TournamentProvider) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash")

    crashing = TournamentProvider(
        drafts=[make_draft("TAKE-ONE"), make_draft("TAKE-TWO"), make_draft("TAKE-THREE")],
        winner_marker="TAKE-TWO",
        judge_hook=crash_on_third_judge_call,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_tournament(project, 1, takes=3, provider=crashing)

    # State on disk is resumable: takes captured, first pair judged.
    states = [
        f.stem for f in (project.root / ".stoner" / "tournaments").glob("ch-01-*.json")
    ]
    assert len(states) == 1
    mid = load_state(project, states[0])
    assert mid.status == "judging"
    assert len(mid.takes) == 3
    assert len(mid.comparisons) == 1

    fresh = TournamentProvider(winner_marker="TAKE-TWO")
    res = run_tournament(project, 1, resume_id=states[0], provider=fresh)
    assert res.status == "proposed"
    assert res.proposed_winner == 2
    assert res.comparisons == 3
    assert fresh.draft_calls == 0  # takes were NOT re-drafted
    assert "tournament.resume" in tournament_actions(project)


# ---------------------------------------------------------------------------
# apply_winner
# ---------------------------------------------------------------------------


def test_apply_with_graft_writes_grafted_body_as_draft(project):
    res = run_tournament(project, 1, takes=3, provider=full_provider())
    graft_provider = TournamentProvider()
    applied = apply_winner(project, res.id, provider=graft_provider)

    assert applied.take == 2
    assert applied.grafted is True
    assert graft_provider.graft_calls == 1
    fm, body = project.read_chapter(1)
    assert "GRAFTED" in body
    assert fm["status"] == "draft"
    assert fm["title"] == "One"  # pre-tournament header survives
    assert load_state(project, res.id).status == "applied"


def test_graft_refusal_falls_back_to_raw_winner_with_note(project):
    res = run_tournament(project, 1, takes=3, provider=full_provider())
    stubby = TournamentProvider(graft_response="fifty words of nothing")
    applied = apply_winner(project, res.id, provider=stubby)

    assert applied.grafted is False
    assert any("graft refused" in n for n in applied.notes)
    _fm, body = project.read_chapter(1)
    assert "TAKE-TWO" in body  # the raw winner, untouched
    assert "tournament.graft" not in tournament_actions(project)


def test_apply_without_graft_writes_raw_winner(project):
    res = run_tournament(project, 1, takes=3, provider=full_provider())
    provider = TournamentProvider()
    applied = apply_winner(project, res.id, graft=False, provider=provider)
    assert applied.grafted is False
    assert provider.graft_calls == 0
    _fm, body = project.read_chapter(1)
    assert "TAKE-TWO" in body


def test_apply_on_drifted_chapter_requires_force(project):
    res = run_tournament(project, 1, takes=3, provider=full_provider())
    fm, _body = project.read_chapter(1)
    project.write_chapter(1, fm, "A hand edit made after the tournament ran.\n")

    with pytest.raises(ValueError, match="--force"):
        apply_winner(project, res.id, graft=False)
    applied = apply_winner(project, res.id, graft=False, force=True)
    assert applied.take == 2
    _fm2, body = project.read_chapter(1)
    assert "TAKE-TWO" in body


def test_apply_take_override_beats_proposal(project):
    res = run_tournament(project, 1, takes=3, provider=full_provider())
    assert res.proposed_winner == 2
    applied = apply_winner(project, res.id, take=1, graft=False)
    assert applied.take == 1
    _fm, body = project.read_chapter(1)
    assert "TAKE-ONE" in body


def test_apply_unknown_or_unfinished_tournament_raises(project):
    with pytest.raises(ValueError, match="no tournament state"):
        apply_winner(project, "ch-01-nope")


def test_ledger_sequence_start_takes_compares_propose_graft_apply(project):
    res = run_tournament(project, 1, takes=3, provider=full_provider())
    apply_winner(project, res.id, provider=TournamentProvider())

    actions = tournament_actions(project)
    assert actions == [
        "tournament.start",
        "tournament.take",
        "tournament.take",
        "tournament.take",
        "tournament.compare",
        "tournament.compare",
        "tournament.compare",
        "tournament.propose",
        "tournament.graft",
        "tournament.apply",
    ]
