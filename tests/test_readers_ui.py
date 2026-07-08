"""Tests for the readers UI endpoints + path jail (U7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.project import WritingProject
from stoner.readers.heatmap import run_heatmap
from stoner.readers.state import ChapterLog, Marker, RunState, save_state
from stoner.types import Span
from stoner.ui.server import _BadPath, _NotFound, _safe_readers_run_dir

BODY = "The strongbox sat there.\n\nThe frost came early that year.\n"


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "mybook", "My Book")
    scaffold_project(proj, "My Book")
    proj.write_chapter(1, {"title": "One"}, BODY)
    return proj


@pytest.fixture()
def client(project: WritingProject):
    from fastapi.testclient import TestClient

    from stoner.ui.server import create_app

    return TestClient(create_app(project))


def _seed_run(project: WritingProject, run_id: str = "run-1") -> None:
    roster = ["owen_shelby", "lena_voss", "maya_riven", "tasha_bloom"]
    log = ChapterLog(
        chapter=1,
        markers=[Marker(persona=p, chapter=1, type="confused", quote="x", span=Span(start=4, end=5, line=1)) for p in roster],
    )
    state = RunState(run_id=run_id, roster=roster, chapters=[1], chapter_logs={1: log})
    save_state(project, state)
    run_heatmap(project, run_id)


# ---------------------------------------------------------------------------
# runs list + detail
# ---------------------------------------------------------------------------


def test_runs_list_empty_returns_empty_list(client):
    res = client.get("/api/readers/runs")
    assert res.status_code == 200
    assert res.json() == []


def test_runs_list_and_detail(project: WritingProject, client):
    _seed_run(project, "run-1")
    res = client.get("/api/readers/runs")
    assert res.status_code == 200
    runs = res.json()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-1"
    assert runs[0]["kind"] == "readers"
    assert runs[0]["has_heatmap"] is True
    assert runs[0]["chapter_range"] == [1, 1]

    detail = client.get("/api/readers/runs/run-1")
    assert detail.status_code == 200
    body = detail.json()
    assert body["heatmap"] is not None
    assert body["heatmap"]["segments"]
    assert body["heatmap"]["findings"]  # unanimous confused -> a trouble segment
    assert "owen_shelby" in body["roster"]


def test_unknown_run_id_404(client):
    res = client.get("/api/readers/runs/nope-999")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# path jail (plain helper, no fastapi)
# ---------------------------------------------------------------------------


def test_path_jail_rejects_traversal(project: WritingProject):
    for bad in ["../../etc", "..", "a/b", "foo/../bar", ".", ""]:
        with pytest.raises(_BadPath):
            _safe_readers_run_dir(project, bad)


def test_path_jail_missing_run_is_notfound(project: WritingProject):
    with pytest.raises(_NotFound):
        _safe_readers_run_dir(project, "run-absent")


def test_traversal_run_id_over_http_rejected(client):
    # a %2e%2e-style id must not escape; fastapi routing keeps it a path param.
    res = client.get("/api/readers/runs/..")
    # either a 400 (bad path) or 404 (routing) -- never a 200 with content.
    assert res.status_code in (400, 404)


# ---------------------------------------------------------------------------
# existing UI unaffected
# ---------------------------------------------------------------------------


def test_index_still_served_with_readers_panel(client):
    res = client.get("/")
    assert res.status_code == 200
    assert 'data-view="readers"' in res.text
    assert 'id="view-readers"' in res.text
