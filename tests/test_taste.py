"""Taste profile tests (U5): vote recording, derived stats, digest, and
angle weighting. Plain JSON counting -- no model calls anywhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.config import TournamentConfig
from stoner.ledger import Ledger
from stoner.project import WritingProject
from stoner.tournament.angles import select_angles
from stoner.tournament.state import Comparison, TakeRecord, TournamentState, save_state
from stoner.tournament.taste import (
    MIN_VOTES_FOR_DIGEST,
    TasteProfile,
    angle_weights,
    digest,
    load_taste,
    record_vote,
    save_taste,
    taste_path,
)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "testbook")
    scaffold_project(p, "testbook")
    return p


def seeded_state(project: WritingProject, verdict: str = "a") -> TournamentState:
    """Two-take tournament with one judged comparison (verdict favors `a`)."""
    state = TournamentState(id="ch-01-1111", chapter=1, status="proposed")
    state.takes = [
        TakeRecord(index=1, angle="dialogue_led", rel_path="x", words=100),
        TakeRecord(index=2, angle="pov_distant", rel_path="y", words=100),
    ]
    state.comparisons = [Comparison(a=1, b=2, verdict=verdict)]  # type: ignore[arg-type]
    save_state(project, state)
    return state


def test_record_vote_persists_history_and_angle_stats(project):
    state = seeded_state(project)
    record_vote(project, state, (1, 2), picked=1)

    data = json.loads(taste_path(project).read_text(encoding="utf-8"))
    assert len(data["votes"]) == 1
    assert data["votes"][0]["picked"] == 1
    assert data["angle_stats"]["dialogue_led"] == {"wins": 1, "losses": 0}
    assert data["angle_stats"]["pov_distant"] == {"wins": 0, "losses": 1}
    # Mirrored onto the tournament state and ledgered.
    assert len(state.votes) == 1
    actions = [e.action for e in Ledger(project.root).tail(50)]
    assert "tournament.vote" in actions


def test_judge_disagreement_counted_per_model(project):
    # Judge picked take 1 (verdict "a"); the human picks take 2.
    state = seeded_state(project, verdict="a")
    record_vote(project, state, (1, 2), picked=2)
    profile = load_taste(project)
    model = project.config.models.reviewer
    assert profile.judge_agreement[model] == {"agree": 0, "disagree": 1}

    # And agreement when they match.
    record_vote(project, state, (1, 2), picked=1)
    profile = load_taste(project)
    assert profile.judge_agreement[model] == {"agree": 1, "disagree": 1}


def test_vote_on_undjudged_pair_records_no_agreement(project):
    state = seeded_state(project)
    state.comparisons = []
    save_state(project, state)
    record_vote(project, state, (1, 2), picked=1)
    profile = load_taste(project)
    assert profile.judge_agreement == {}


def test_vote_validation(project):
    state = seeded_state(project)
    with pytest.raises(ValueError, match="not in pair"):
        record_vote(project, state, (1, 2), picked=9)


def test_digest_empty_below_minimum_then_populated_and_capped(project):
    state = seeded_state(project)
    for _ in range(MIN_VOTES_FOR_DIGEST - 1):
        record_vote(project, state, (1, 2), picked=1)
    assert digest(load_taste(project)) == ""

    record_vote(project, state, (1, 2), picked=1)
    text = digest(load_taste(project))
    assert "dialogue_led 5-0" in text
    assert "pov_distant 0-5" in text
    assert "agreed with the writer" in text
    assert len(text) <= 600
    # A tight cap truncates rather than exceeding the budget.
    assert len(digest(load_taste(project), max_chars=50)) <= 50


def test_angle_weights_reorder_selection_after_lopsided_votes(project):
    cfg = TournamentConfig()
    # No votes: stable preset order, empty weights.
    assert angle_weights(load_taste(project)) == {}
    assert [a.name for a in select_angles(2, cfg)] == ["in_scene", "aftermath"]

    state = seeded_state(project)
    for _ in range(MIN_VOTES_FOR_DIGEST):
        record_vote(project, state, (1, 2), picked=1)  # dialogue_led sweeps
    weights = angle_weights(load_taste(project))
    assert weights["dialogue_led"] == 1.0
    assert weights["pov_distant"] == 0.0
    picked = select_angles(3, cfg, weights=weights)
    assert picked[0].name == "dialogue_led"
    assert "pov_distant" not in [a.name for a in picked[:2]]


def test_corrupt_taste_backed_up_and_fresh_profile(project):
    p = taste_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{broken", encoding="utf-8")
    profile = load_taste(project)
    assert profile.votes == []
    assert (p.parent / (p.name + ".bak")).exists()
    # And the fresh profile saves cleanly as human-readable JSON.
    save_taste(project, TasteProfile())
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "votes": [],
        "angle_stats": {},
        "judge_agreement": {},
    }
