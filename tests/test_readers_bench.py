"""Tests for comp ingestion + blind pairwise benchmark (U5). No network."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.readers import bench as bench_mod
from stoner.readers.bench import run_bench
from stoner.readers.comps import add_comp, list_comps, load_comp_chapters, split_chapters
from stoner.readers.state import load_state
from stoner.types import CompletionRequest, CompletionResponse, Usage

ROSTER = ["maya_riven", "owen_shelby", "marcus_hale", "lena_voss"]

COMP_TEXT = (
    "Chapter 1\n\nThe comp opens on a quiet harbor at dawn.\n\n"
    "Chapter 2\n\nBy the second chapter the comp has found its feet.\n"
)


class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self, responder: Callable[[int, CompletionRequest], CompletionResponse]):
        self.responder = responder
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        return self.responder(idx, req)


def _persona_ids_in(req: CompletionRequest, roster: list[str]) -> list[str]:
    body = req.messages[0].content
    return [pid for pid in roster if f"id: {pid}" in body]


def _all_pick_a(idx, req):
    ids = _persona_ids_in(req, ROSTER)
    readers = {pid: {"pick": "a", "loss_a": [], "loss_b": [], "note": "A held me"} for pid in ids}
    return CompletionResponse(text=json.dumps({"readers": readers}), usage=Usage(input_tokens=3, output_tokens=2))


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Book")
    scaffold_project(proj, "Book")
    proj.write_chapter(1, {"title": "One"}, "The manuscript begins in a counting room.\n")
    proj.write_chapter(2, {"title": "Two"}, "The manuscript turns on a forged letter.\n")
    return proj


def _comp_file(tmp_path: Path, text: str = COMP_TEXT) -> Path:
    p = tmp_path / "comp_source.txt"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# comp ingestion
# ---------------------------------------------------------------------------


def test_add_comp_splits_on_headings_and_refuses_overwrite(project: WritingProject, tmp_path: Path):
    meta = add_comp(
        project, _comp_file(tmp_path), title="Harbor Tales", author="A. Writer", year="1901", source="Gutenberg"
    )
    assert meta.slug == "harbor-tales"
    assert meta.chapters == 2
    cdir = project.root / "comps" / "harbor-tales"
    assert (cdir / "ch-01.md").exists() and (cdir / "ch-02.md").exists()
    assert "quiet harbor" in (cdir / "ch-01.md").read_text(encoding="utf-8")
    assert (cdir / "comp.json").exists()
    assert [m.slug for m in list_comps(project)] == ["harbor-tales"]

    with pytest.raises(ValueError, match="already exists"):
        add_comp(project, _comp_file(tmp_path), title="Harbor Tales")
    # force overwrites.
    add_comp(project, _comp_file(tmp_path), title="Harbor Tales", force=True)


def test_headingless_text_falls_back_to_wordcount():
    text = " ".join(["word"] * 3500)  # no chapter headings
    chunks = split_chapters(text)
    assert len(chunks) == 3  # 1500 + 1500 + 500
    assert all(chunks)


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------


def test_bench_blind_mapping_recorded_before_calls_and_decoded(project: WritingProject, tmp_path: Path):
    add_comp(project, _comp_file(tmp_path), title="Comp")
    provider = ScriptedProvider(_all_pick_a)
    result = run_bench(project, "comp", roster=ROSTER, provider=provider, run_id="bench-1")

    assert result.chapters_aligned == 2
    assert result.calls == 2  # 2 chapters x 1 batch (k=4)

    state = load_state(project, "bench-1")
    # blind map covers every aligned chapter (was saved before any call).
    assert set(state.blind_map) == {1, 2}

    # every persona picked "a"; decode must honor the per-chapter slot.
    for row in result.per_chapter:
        slot = state.blind_map[row["chapter"]]
        if slot == "a":
            assert row["manuscript"] == 4 and row["comp"] == 0 and row["win_rate"] == 1.0
        else:
            assert row["comp"] == 4 and row["manuscript"] == 0 and row["win_rate"] == 0.0


def test_bench_win_rate_matches_hand_computation(project: WritingProject, tmp_path: Path):
    add_comp(project, _comp_file(tmp_path), title="Comp")

    # personas always pick the manuscript, whichever blind slot it occupies.
    def pick_manuscript(idx, req):
        ids = _persona_ids_in(req, ROSTER)
        # the manuscript passage mentions "manuscript"; find its slot in the prompt.
        body = req.messages[0].content
        a_start = body.index("## Passage A")
        b_start = body.index("## Passage B")
        passage_a = body[a_start:b_start]
        slot = "a" if "manuscript" in passage_a else "b"
        readers = {pid: {"pick": slot, "loss_a": [], "loss_b": []} for pid in ids}
        return CompletionResponse(text=json.dumps({"readers": readers}), usage=Usage())

    result = run_bench(project, "comp", roster=ROSTER, provider=ScriptedProvider(pick_manuscript), run_id="bench-2")
    # manuscript wins every decisive pick -> 100% win rate everywhere.
    assert result.manuscript_win_rate == 1.0
    assert all(row["win_rate"] == 1.0 for row in result.per_chapter)


def test_bench_comp_shorter_covers_aligned_only(project: WritingProject, tmp_path: Path):
    # manuscript has 3 chapters; comp has 2.
    project.write_chapter(3, {"title": "Three"}, "The manuscript closes on open water.\n")
    add_comp(project, _comp_file(tmp_path), title="Comp")
    result = run_bench(project, "comp", roster=ROSTER, provider=ScriptedProvider(_all_pick_a), run_id="bench-3")
    assert result.chapters_aligned == 2
    assert result.comp_shorter is True
    assert any("aligned" in n for n in result.notes)
    assert len(result.per_chapter) == 2


def test_bench_uses_tournament_elo_when_present(project: WritingProject, tmp_path: Path):
    add_comp(project, _comp_file(tmp_path), title="Comp")
    result = run_bench(project, "comp", roster=ROSTER, provider=ScriptedProvider(_all_pick_a), run_id="bench-4")
    # tournament.rating exists on this branch -> Elo aggregation.
    assert result.aggregation == "elo"
    assert set(result.ratings) == {"manuscript", "comp"}


def test_bench_degrades_when_rating_absent(project: WritingProject, tmp_path: Path, monkeypatch):
    add_comp(project, _comp_file(tmp_path), title="Comp")
    monkeypatch.setattr(bench_mod, "_import_rating", lambda: None)
    result = run_bench(project, "comp", roster=ROSTER, provider=ScriptedProvider(_all_pick_a), run_id="bench-5")
    assert result.aggregation == "win-rate"
    assert any("win-rate" in n for n in result.notes)
    assert result.ratings == {}


def test_bench_writes_bench_json(project: WritingProject, tmp_path: Path):
    add_comp(project, _comp_file(tmp_path), title="Comp")
    run_bench(project, "comp", roster=ROSTER, provider=ScriptedProvider(_all_pick_a), run_id="bench-6")
    bj = project.root / ".stoner" / "readers" / "runs" / "bench-6" / "bench.json"
    assert bj.exists()
    data = json.loads(bj.read_text(encoding="utf-8"))
    assert data["kind"] == "bench"
    assert data["comp"] == "comp"
    assert set(data["blind_map"]) == {"1", "2"}


def test_load_comp_chapters_roundtrip(project: WritingProject, tmp_path: Path):
    add_comp(project, _comp_file(tmp_path), title="Comp")
    chapters = load_comp_chapters(project, "comp")
    assert len(chapters) == 2
    assert "quiet harbor" in chapters[0]
