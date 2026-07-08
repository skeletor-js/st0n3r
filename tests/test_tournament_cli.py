"""CLI tests for `stoner tournament ...` (CliRunner, no network).

`run` is only exercised for argument plumbing (monkeypatched orchestrator);
the pipeline itself is covered in test_tournament_run.py /
test_tournament_graft.py. vote/list/status/apply run against pre-seeded
state files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.cli.main import app
from stoner.project import WritingProject
from stoner.tournament.state import Comparison, TakeRecord, TournamentState, save_state, takes_dir

runner = CliRunner()

NEUTRAL_BODY_A = "He crossed the yard and said nothing. The gate sagged on its hinge.\n"
NEUTRAL_BODY_B = "The kettle sat cold. She counted the hours by the window light.\n"


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    result = runner.invoke(app, ["init", "mybook", "--path", str(tmp_path / "mybook")])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path / "mybook")
    return tmp_path / "mybook"


def seed_tournament(root: Path, tid: str = "ch-01-1111", status: str = "proposed") -> TournamentState:
    project = WritingProject.find(root)
    state = TournamentState(id=tid, chapter=1, status=status)  # type: ignore[arg-type]
    for idx, (angle, body) in enumerate(
        (("dialogue_led", NEUTRAL_BODY_A), ("pov_distant", NEUTRAL_BODY_B)), start=1
    ):
        rel = str((takes_dir(project, tid) / f"take-{idx:02d}.md").relative_to(project.root))
        project.write(rel, f"---\nangle: {angle}\n---\n\n{body}")
        state.takes.append(
            TakeRecord(index=idx, angle=angle, rel_path=rel, words=len(body.split()))
        )
    state.comparisons = [Comparison(a=1, b=2, verdict="a")]
    state.ratings = {1: 1216.0, 2: 1184.0}
    state.proposed_winner = 1
    save_state(project, state)
    return state


def test_tournament_help_lists_all_commands():
    result = runner.invoke(app, ["tournament", "--help"])
    assert result.exit_code == 0
    for cmd in ("run", "list", "status", "vote", "apply"):
        assert cmd in result.output


def test_list_shows_seeded_tournaments(project_dir: Path):
    seed_tournament(project_dir)
    seed_tournament(project_dir, tid="ch-02-2222", status="judging")
    result = runner.invoke(app, ["tournament", "list"])
    assert result.exit_code == 0, result.output
    assert "ch-01-1111" in result.output
    assert "ch-02-2222" in result.output
    assert "proposed" in result.output


def test_status_shows_standings_and_budget(project_dir: Path):
    seed_tournament(project_dir)
    result = runner.invoke(app, ["tournament", "status", "ch-01-1111"])
    assert result.exit_code == 0, result.output
    assert "dialogue_led" in result.output
    assert "1216" in result.output
    assert "judge calls" in result.output


def test_unknown_id_exits_one_with_actionable_message(project_dir: Path):
    result = runner.invoke(app, ["tournament", "status", "ch-09-nope"])
    assert result.exit_code == 1
    assert "tournament list" in result.output


def test_vote_records_taste_and_reveals_angle_only_after_pick(project_dir: Path):
    seed_tournament(project_dir)
    result = runner.invoke(app, ["tournament", "vote", "ch-01-1111", "--pairs", "1"], input="a\n")
    assert result.exit_code == 0, result.output

    taste = json.loads((project_dir / ".stoner" / "taste.json").read_text(encoding="utf-8"))
    assert len(taste["votes"]) == 1
    assert taste["votes"][0]["tournament_id"] == "ch-01-1111"

    # Blindness: the angle name appears only AFTER the pick prompt.
    prompt_pos = result.output.index("Your pick")
    assert "dialogue_led" in result.output
    assert result.output.index("dialogue_led") > prompt_pos
    assert result.output.index("judge verdict") > prompt_pos


def test_vote_skip_records_nothing(project_dir: Path):
    seed_tournament(project_dir)
    result = runner.invoke(app, ["tournament", "vote", "ch-01-1111", "--pairs", "1"], input="s\n")
    assert result.exit_code == 0, result.output
    assert not (project_dir / ".stoner" / "taste.json").exists()


def test_run_passes_flags_through_to_orchestrator(project_dir: Path, monkeypatch):
    from stoner.tournament.run import TournamentResult

    captured: dict = {}

    def fake_run_tournament(project, chapter, **kwargs):
        captured["chapter"] = chapter
        captured.update(kwargs)
        return TournamentResult(id="ch-01-9", chapter=chapter, takes=7, status="proposed")

    monkeypatch.setattr("stoner.tournament.run.run_tournament", fake_run_tournament)
    result = runner.invoke(
        app,
        ["tournament", "run", "1", "--takes", "7", "--max-comparisons", "9", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert captured["chapter"] == 1
    assert captured["takes"] == 7
    assert captured["max_comparisons"] == 9
    assert captured["force"] is True


def test_apply_confirms_and_passes_take_and_force(project_dir: Path, monkeypatch):
    from stoner.tournament.run import ApplyResult

    seed_tournament(project_dir)
    captured: dict = {}

    def fake_apply_winner(project, tournament_id, **kwargs):
        captured["id"] = tournament_id
        captured.update(kwargs)
        return ApplyResult(id=tournament_id, chapter=1, take=kwargs.get("take") or 1, words=100)

    monkeypatch.setattr("stoner.tournament.run.apply_winner", fake_apply_winner)

    # Declining the confirmation applies nothing.
    result = runner.invoke(app, ["tournament", "apply", "ch-01-1111"], input="n\n")
    assert result.exit_code == 0
    assert "id" not in captured

    result = runner.invoke(
        app,
        ["tournament", "apply", "ch-01-1111", "--take", "2", "--force", "--no-graft", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert captured["id"] == "ch-01-1111"
    assert captured["take"] == 2
    assert captured["force"] is True
    assert captured["graft"] is False
