"""Tournament core tests (U1): Elo math, pairing, state persistence, config,
slot sizing, and angle selection. Pure deterministic code -- no model calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.config import StonerConfig, TournamentConfig
from stoner.project import WritingProject
from stoner.tournament import angles as angles_mod
from stoner.tournament import rating
from stoner.tournament.state import (
    TakeRecord,
    TournamentState,
    load_state,
    save_state,
    state_path,
)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "testbook")
    scaffold_project(p, "testbook")
    return p


# ---------------------------------------------------------------------------
# Elo
# ---------------------------------------------------------------------------


def test_elo_draw_between_equals_changes_nothing():
    ra, rb = rating.update(1200.0, 1200.0, rating.DRAW)
    assert ra == pytest.approx(1200.0)
    assert rb == pytest.approx(1200.0)


def test_elo_update_symmetry():
    ra, rb = rating.update(1200.0, 1200.0, rating.WIN)
    assert ra == pytest.approx(1216.0)
    assert rb == pytest.approx(1184.0)
    # Zero-sum: the pool of rating points is conserved.
    ra2, rb2 = rating.update(1300.0, 1100.0, rating.LOSS)
    assert ra2 + rb2 == pytest.approx(1300.0 + 1100.0)


def test_elo_upset_moves_more_points_than_expected_win():
    strong_wins, _ = rating.update(1400.0, 1000.0, rating.WIN)
    weak_wins, _ = rating.update(1000.0, 1400.0, rating.WIN)
    assert (weak_wins - 1000.0) > (strong_wins - 1400.0)


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def test_round_robin_pair_counts():
    assert len(rating.round_robin_pairs([1, 2, 3])) == 3
    assert len(rating.round_robin_pairs([1, 2, 3, 4])) == 6
    assert rating.round_robin_pairs([2, 1]) == [(1, 2)]


def test_swiss_pairs_adjacent_standings_no_rematches():
    ratings = {1: 1300.0, 2: 1280.0, 3: 1260.0, 4: 1240.0, 5: 1220.0, 6: 1200.0}
    first = rating.swiss_pairs(ratings, set())
    assert first == [(1, 2), (3, 4), (5, 6)]  # adjacent by standing

    played = {frozenset(p) for p in first}
    second = rating.swiss_pairs(ratings, played)
    assert second  # a second round exists
    for a, b in second:
        assert frozenset((a, b)) not in played  # no rematches
    # Everyone still gets paired.
    used = {i for p in second for i in p}
    assert used == set(ratings)


def test_swiss_rounds_formula():
    assert rating.swiss_rounds(5) == 4  # ceil(log2 5) + 1
    assert rating.swiss_rounds(8) == 4
    assert rating.swiss_rounds(2) == 2


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_state_roundtrip_and_int_rating_keys(project):
    state = TournamentState(id="ch-01-1", chapter=1, ratings={1: 1216.0, 2: 1184.0})
    state.takes.append(TakeRecord(index=1, angle="in_scene", rel_path="x", words=10))
    save_state(project, state)
    loaded = load_state(project, "ch-01-1")
    assert loaded.ratings == {1: 1216.0, 2: 1184.0}
    assert loaded.takes[0].angle == "in_scene"


def test_corrupt_state_backed_up_and_fresh_returned(project):
    p = state_path(project, "ch-01-9")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    state = load_state(project, "ch-01-9")
    assert state.takes == [] and state.status == "drafting"
    assert (p.parent / (p.name + ".bak")).exists()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_roundtrips_tournament_block(tmp_path: Path):
    (tmp_path / "stoner.yaml").write_text(
        "project_name: t\n"
        "tournament:\n"
        "  takes: 4\n"
        "  slot_takes: {opening: 7, ending: 6}\n"
        "  max_comparisons: 10\n"
        "  graft: false\n"
        "  angles:\n"
        "    - {name: fever_dream, instruction: Write it as a fever dream.}\n",
        encoding="utf-8",
    )
    cfg = StonerConfig.load(tmp_path)
    assert cfg.tournament.takes == 4
    assert cfg.tournament.slot_takes == {"opening": 7, "ending": 6}
    assert cfg.tournament.max_comparisons == 10
    assert cfg.tournament.graft is False
    assert cfg.tournament.angles[0]["name"] == "fever_dream"


def test_config_defaults():
    cfg = TournamentConfig()
    assert cfg.takes == 3
    assert cfg.slot_takes == {"opening": 5, "ending": 5}
    assert cfg.max_comparisons == 24
    assert cfg.graft is True


# ---------------------------------------------------------------------------
# Slot sizing
# ---------------------------------------------------------------------------


def test_slot_sizing_opening_and_ending(project):
    # scaffold ships beats for ch-01 only; add ch-03 so the ending slot exists.
    project.write("outline/beats/ch-03.md", "---\nchapter: 3\n---\n\nbeats")
    assert angles_mod.takes_for_chapter(project, 1) == 5  # opening
    assert angles_mod.takes_for_chapter(project, 3) == 5  # ending
    assert angles_mod.takes_for_chapter(project, 2) == 3  # default
    assert angles_mod.takes_for_chapter(project, 1, override=2) == 2


def test_takes_override_must_be_at_least_two(project):
    with pytest.raises(ValueError):
        angles_mod.takes_for_chapter(project, 1, override=1)


# ---------------------------------------------------------------------------
# Angle selection
# ---------------------------------------------------------------------------


def test_select_angles_distinct_and_ordered():
    cfg = TournamentConfig()
    picked = angles_mod.select_angles(3, cfg)
    names = [a.name for a in picked]
    assert len(set(names)) == 3
    assert names == [a.name for a in angles_mod.PRESET_ANGLES[:3]]


def test_select_angles_honors_config_defined():
    cfg = TournamentConfig(angles=[{"name": "fever_dream", "instruction": "As a fever dream."}])
    pool = angles_mod.all_angles(cfg)
    assert pool[-1].name == "fever_dream"
    picked = angles_mod.select_angles(len(pool), cfg)
    assert "fever_dream" in [a.name for a in picked]


def test_select_angles_weights_reorder_deterministically():
    cfg = TournamentConfig()
    weighted = angles_mod.select_angles(2, cfg, weights={"image_first": 0.9, "in_scene": 0.1})
    assert weighted[0].name == "image_first"
    # No weights: stable preset order.
    assert [a.name for a in angles_mod.select_angles(2, cfg)] == ["in_scene", "aftermath"]
