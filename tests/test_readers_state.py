"""Tests for reader run state + config (U2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.config import StonerConfig
from stoner.project import WritingProject
from stoner.readers.state import (
    MEMORY_CAP,
    ChapterLog,
    Marker,
    ReaderState,
    RunState,
    load_state,
    new_run_id,
    save_state,
    state_path,
)
from stoner.types import Span


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Book")
    scaffold_project(proj, "Book")
    return proj


def _sample_state(run_id: str) -> RunState:
    state = RunState(run_id=run_id, chapters=[1, 2], roster=["a", "b"])
    st = state.ensure_reader("a")
    st.remember(1, "the estate is quiet")
    st.expectations = ["something will break the quiet"]
    st.fatigue = "a little bored"
    state.chapter_logs[1] = ChapterLog(
        chapter=1,
        markers=[Marker(persona="a", chapter=1, type="hooked", quote="the strongbox", span=Span(start=0, end=13, line=1))],
        reactions={"a": "intrigued"},
    )
    state.budget.calls_made = 1
    return state


# ---------------------------------------------------------------------------
# round-trip + corruption
# ---------------------------------------------------------------------------


def test_save_then_load_round_trips(project: WritingProject):
    rid = new_run_id()
    original = _sample_state(rid)
    save_state(project, original)
    loaded = load_state(project, rid)
    assert loaded.model_dump() == original.model_dump()
    # int chapter keys survive the JSON string-key round-trip.
    assert 1 in loaded.chapter_logs
    assert loaded.chapter_logs[1].markers[0].span.start == 0


def test_corrupt_state_backed_up_and_fresh_returned(project: WritingProject):
    rid = new_run_id()
    save_state(project, _sample_state(rid))
    sp = state_path(project, rid)
    sp.write_text("{ not valid json", encoding="utf-8")
    fresh = load_state(project, rid)
    assert fresh.run_id == rid
    assert fresh.chapter_logs == {}
    assert (sp.parent / (sp.name + ".bak")).exists()


def test_missing_state_returns_fresh(project: WritingProject):
    fresh = load_state(project, "run-does-not-exist")
    assert fresh.run_id == "run-does-not-exist"
    assert fresh.chapters == []


# ---------------------------------------------------------------------------
# memory trimming
# ---------------------------------------------------------------------------


def test_reader_memory_drops_oldest_past_cap():
    st = ReaderState(persona="a")
    for n in range(1, 60):
        st.remember(n, "x" * 80, cap=500)
    assert len(st.memory) <= 500
    # oldest lines dropped: the first chapters are gone, the latest survives.
    assert "[ch 1]" not in st.memory
    assert "[ch 59]" in st.memory


def test_reader_memory_keeps_last_line_even_if_oversized():
    st = ReaderState(persona="a")
    st.remember(1, "y" * (MEMORY_CAP + 500))
    assert st.memory.startswith("[ch 1]")
    assert "y" in st.memory


def test_remember_ignores_blank_lines():
    st = ReaderState(persona="a")
    st.remember(1, "   ")
    assert st.memory == ""


# ---------------------------------------------------------------------------
# config defaults
# ---------------------------------------------------------------------------


def test_readers_config_defaults_resolve(tmp_path: Path):
    # No `readers` block in stoner.yaml still yields defaults.
    cfg = StonerConfig.load(tmp_path)
    assert cfg.readers.roster == []
    assert cfg.readers.roster_size == 12
    assert cfg.readers.personas_per_call == 4
    assert cfg.readers.max_calls_per_run == 150
    assert cfg.readers.agreement_threshold == 0.5


def test_reader_model_role_default_is_haiku_class():
    cfg = StonerConfig()
    assert cfg.models.reader == "anthropic/claude-haiku-4-5-20251001"
    # round-trips through the yaml dump without dropping the new field.
    dumped = cfg.dump_yaml()
    assert "reader:" in dumped
    assert "readers:" in dumped
