"""Tests for the heatmap aggregation (U4). Pure arithmetic, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.project import WritingProject
from stoner.readers.heatmap import build_heatmap, paragraph_segments, run_heatmap
from stoner.readers.personas import load_shipped
from stoner.readers.state import ChapterLog, Marker, MarkerType, RunState, save_state
from stoner.types import Span

BODY = "The strongbox sat there.\n\nThe frost came early that year.\n"
# paragraph 0: chars [0, 24); paragraph 1 starts at 26.


def _marker(persona: str, mtype: MarkerType, offset: int) -> Marker:
    return Marker(persona=persona, chapter=1, type=mtype, quote="x", span=Span(start=offset, end=offset + 1, line=1))


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Book")
    scaffold_project(proj, "Book")
    proj.write_chapter(1, {"title": "One"}, BODY)
    return proj


def _personas(ids: list[str]):
    shipped = load_shipped(force_reload=True)
    return {i: shipped[i] for i in ids}


# ---------------------------------------------------------------------------
# segmentation
# ---------------------------------------------------------------------------


def test_paragraph_segments_offsets():
    segs = paragraph_segments(BODY)
    assert len(segs) == 2
    i0, s0, e0, t0 = segs[0]
    assert (s0, t0) == (0, "The strongbox sat there.")
    assert BODY[s0:e0] == "The strongbox sat there."
    i1, s1, e1, t1 = segs[1]
    assert t1 == "The frost came early that year."
    assert BODY[s1:e1] == t1


# ---------------------------------------------------------------------------
# hand-computed aggregation
# ---------------------------------------------------------------------------


def test_segment_stats_match_hand_computation():
    roster = ["owen_shelby", "lena_voss", "nadia_kass", "maya_riven", "tasha_bloom", "jamal_price"]
    personas = _personas(roster)
    log = ChapterLog(chapter=1)
    # paragraph 0: 3 hooked (offset 4), 1 bored
    log.markers += [_marker(roster[0], "hooked", 4), _marker(roster[1], "hooked", 5), _marker(roster[2], "hooked", 6)]
    log.markers += [_marker(roster[3], "bored", 7)]
    # paragraph 1: 2 confused (offset 30)
    log.markers += [_marker(roster[4], "confused", 30), _marker(roster[5], "confused", 31)]
    state = RunState(run_id="r1", roster=roster, chapter_logs={1: log})

    report = build_heatmap(state, {1: BODY}, personas, agreement_threshold=0.5)
    assert report.roster_size == 6
    p0 = report.segments[0]
    assert p0.counts == {"hooked": 3, "bored": 1, "confused": 0, "reread": 0}
    assert p0.attention == pytest.approx(0.333, abs=1e-3)  # (3-1)/6
    assert p0.agreement == pytest.approx(0.667, abs=1e-3)  # 4 distinct / 6
    assert p0.negative_agreement == pytest.approx(0.167, abs=1e-3)  # 1/6
    p1 = report.segments[1]
    assert p1.attention == pytest.approx(-0.333, abs=1e-3)  # (0-2)/6
    assert p1.negative_agreement == pytest.approx(0.333, abs=1e-3)
    # nothing clears the 0.5 negative-agreement threshold.
    assert report.findings == []


def test_unanimous_confused_produces_one_major_finding_with_span():
    roster = ["owen_shelby", "lena_voss", "maya_riven", "tasha_bloom"]
    personas = _personas(roster)
    log = ChapterLog(chapter=1, markers=[_marker(pid, "confused", 4) for pid in roster])
    state = RunState(run_id="r1", roster=roster, chapter_logs={1: log})

    report = build_heatmap(state, {1: BODY}, personas, agreement_threshold=0.5)
    assert len(report.findings) == 1
    f = report.findings[0]
    assert f.source == "readers:heatmap"
    assert f.severity.value == "major"
    assert f.span is not None
    # span lands inside paragraph 0 (chars 0..24).
    assert 0 <= f.span.start < 24
    assert f.id == "rh-01-0"


def test_disagreement_names_splitting_attribute():
    high = ["owen_shelby", "lena_voss", "nadia_kass"]  # patience high
    low = ["maya_riven", "tasha_bloom", "jamal_price"]  # patience low
    roster = high + low
    personas = _personas(roster)
    log = ChapterLog(chapter=1)
    log.markers += [_marker(pid, "hooked", 4) for pid in high]
    log.markers += [_marker(pid, "bored", 5) for pid in low]
    state = RunState(run_id="r1", roster=roster, chapter_logs={1: log})

    report = build_heatmap(state, {1: BODY}, personas, agreement_threshold=0.9)
    seg0 = report.segments[0]
    attrs = {d["attribute"] for d in seg0.disagreements}
    assert "patience" in attrs
    patience = next(d for d in seg0.disagreements if d["attribute"] == "patience")
    assert patience["split"]["high"] == pytest.approx(1.0)
    assert patience["split"]["low"] == pytest.approx(-1.0)


def test_null_span_markers_aggregate_at_chapter_level():
    roster = ["owen_shelby", "lena_voss"]
    personas = _personas(roster)
    log = ChapterLog(
        chapter=1,
        markers=[
            Marker(persona="owen_shelby", chapter=1, type="confused", quote="", span=None),
            _marker("lena_voss", "hooked", 4),
        ],
    )
    state = RunState(run_id="r1", roster=roster, chapter_logs={1: log})
    report = build_heatmap(state, {1: BODY}, personas, agreement_threshold=0.5)
    # segment math didn't crash; the null-span confused landed at chapter level.
    assert report.chapter_markers[1]["confused"] == 1
    assert report.segments[0].counts["hooked"] == 1


def test_empty_run_valid_empty_heatmap():
    state = RunState(run_id="r1", roster=["owen_shelby"], chapter_logs={})
    report = build_heatmap(state, {}, _personas(["owen_shelby"]), agreement_threshold=0.5)
    assert report.segments == []
    assert report.findings == []
    assert any("empty" in n for n in report.notes)


# ---------------------------------------------------------------------------
# mirroring into .stoner/reviews/ + PATCH round-trip
# ---------------------------------------------------------------------------


def test_high_agreement_findings_mirrored_and_patchable(project: WritingProject):
    roster = ["owen_shelby", "lena_voss", "maya_riven", "tasha_bloom"]
    log = ChapterLog(chapter=1, markers=[_marker(pid, "confused", 4) for pid in roster])
    state = RunState(run_id="run-x", roster=roster, chapter_logs={1: log})
    save_state(project, state)

    report, paths = run_heatmap(project, "run-x")
    assert report.findings
    assert "review_json" in paths

    review_file = Path(paths["review_json"]).name
    data = json.loads(Path(paths["review_json"]).read_text(encoding="utf-8"))
    assert data["kind"] == "readers"
    assert len(data["findings"]) == len(report.findings)
    finding_id = data["findings"][0]["id"]

    from fastapi.testclient import TestClient

    from stoner.ui.server import create_app

    client = TestClient(create_app(project))
    res = client.patch(f"/api/reviews/{review_file}/findings/{finding_id}", json={"status": "accepted"})
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"
    # persisted
    again = json.loads(Path(paths["review_json"]).read_text(encoding="utf-8"))
    assert again["findings"][0]["status"] == "accepted"


def test_below_threshold_segments_not_mirrored(project: WritingProject):
    # a single bored marker in a 4-person roster: negative agreement 0.25 < 0.5.
    roster = ["owen_shelby", "lena_voss", "maya_riven", "tasha_bloom"]
    log = ChapterLog(chapter=1, markers=[_marker("owen_shelby", "bored", 4)])
    state = RunState(run_id="run-y", roster=roster, chapter_logs={1: log})
    save_state(project, state)
    report, paths = run_heatmap(project, "run-y")
    assert report.findings == []
    assert "review_json" not in paths
    # no mirrored readers-*.json exists.
    assert not list((project.root / ".stoner" / "reviews").glob("readers-*.json"))
