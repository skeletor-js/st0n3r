"""Tests for the Writers' Room UI endpoints (U7): sessions listing/detail
(path-jailed), notebooks, and comment POST/PATCH (tests/test_ui.py patterns)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.project import WritingProject

BODY = (
    "The harbor bell rang twice before Mara reached the quay. She counted\n"
    "the crates herself, twice, and the count came up short both times.\n"
)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "mybook", "My Book")
    scaffold_project(proj, "My Book")
    proj.write_chapter(1, {"title": "Arrival"}, BODY)
    return proj


@pytest.fixture()
def client(project: WritingProject):
    from fastapi.testclient import TestClient

    from stoner.ui.server import create_app

    return TestClient(create_app(project))


def _write_session_record(project: WritingProject, filename: str, **overrides) -> Path:
    record = {
        "id": filename.removesuffix(".json"),
        "scope": "chapter",
        "chapter": 1,
        "editors": ["dev-editor", "line-editor"],
        "relocation": [],
        "findings": [
            {
                "id": "f_1",
                "source": "room:dev-editor:pacing",
                "severity": "major",
                "category": "test",
                "quote": "the count came up short",
                "issue": "sags here",
                "suggestion": "tighten",
                "status": "open",
            }
        ],
        "takes": {},
        "cross_exam": [
            {
                "editor": "line-editor",
                "agreements": [],
                "disagreements": [{"finding_id": "f_1", "note": "disagree"}],
                "priority_rank": [],
                "comment_responses": [],
                "notebook_note": "",
            }
        ],
        "obligations_unmet": [],
        "model": "fake/model",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "created_at": 1700000000.0,
        "notes": [],
    }
    record.update(overrides)
    dest = project.root / ".stoner" / "room" / "sessions" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(record), encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def test_room_sessions_empty(client):
    res = client.get("/api/room/sessions")
    assert res.status_code == 200
    assert res.json() == []


def test_room_sessions_list_newest_first(client, project: WritingProject):
    _write_session_record(project, "ch-01-100.json", created_at=100.0)
    _write_session_record(project, "ch-01-200.json", created_at=200.0)
    res = client.get("/api/room/sessions")
    assert res.status_code == 200
    listing = res.json()
    assert [r["file"] for r in listing] == ["ch-01-200.json", "ch-01-100.json"]
    assert listing[0]["findings"] == 1
    assert listing[0]["editors"] == ["dev-editor", "line-editor"]


def test_room_session_detail(client, project: WritingProject):
    _write_session_record(project, "ch-01-100.json")
    res = client.get("/api/room/sessions/ch-01-100.json")
    assert res.status_code == 200
    data = res.json()
    assert data["findings"][0]["source"] == "room:dev-editor:pacing"
    assert data["cross_exam"][0]["disagreements"][0]["note"] == "disagree"


def test_room_session_detail_missing_404(client):
    res = client.get("/api/room/sessions/does-not-exist.json")
    assert res.status_code == 404


@pytest.mark.parametrize("bad_file", ["../stoner.yaml", "/etc/passwd", "..", "sub/dir.json"])
def test_room_session_detail_rejects_traversal(client, bad_file):
    res = client.get(f"/api/room/sessions/{bad_file}")
    assert res.status_code in (400, 404)  # jail (400) or routing itself (404)


# ---------------------------------------------------------------------------
# notebooks
# ---------------------------------------------------------------------------


def test_room_notebooks_empty(client):
    res = client.get("/api/room/notebooks")
    assert res.status_code == 200
    assert res.json() == []


def test_room_notebooks_lists_editor_state(client, project: WritingProject):
    from stoner.room.notebook import Notebook

    nb = Notebook(project, "line-editor", project.config.room)
    nb.set_opinion("prose tightening nicely")
    nb.upsert_item("f_1", 1, "q", "an issue", "minor", "line", "s1")
    res = client.get("/api/room/notebooks")
    assert res.status_code == 200
    listing = res.json()
    assert len(listing) == 1
    assert listing[0]["editor"] == "line-editor"
    assert listing[0]["opinion"] == "prose tightening nicely"
    assert listing[0]["items"][0]["id"] == "f_1"


# ---------------------------------------------------------------------------
# comments
# ---------------------------------------------------------------------------


def test_room_comment_post_anchors_and_persists(client, project: WritingProject):
    res = client.post(
        "/api/room/comments/1",
        json={"quote": "the count came up short", "text": "too flat?"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["span"] is not None
    assert data["status"] == "open"
    # file exists on disk
    assert (project.root / ".stoner" / "room" / "comments" / "ch-01.json").exists()
    # GET returns it with the span
    res = client.get("/api/room/comments/1")
    assert res.status_code == 200
    listing = res.json()
    assert listing[0]["id"] == data["id"]
    assert listing[0]["span"]["start"] == data["span"]["start"]


def test_room_comment_post_unanchored_quote_notes_warning(client):
    res = client.post(
        "/api/room/comments/1", json={"quote": "text nowhere in chapter", "text": "hm"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["span"] is None
    assert "not found" in data["note"]


def test_room_comment_patch_status_persists(client, project: WritingProject):
    created = client.post("/api/room/comments/1", json={"quote": "", "text": "q"}).json()
    res = client.patch(
        f"/api/room/comments/1/{created['id']}", json={"status": "resolved"}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "resolved"
    on_disk = json.loads(
        (project.root / ".stoner" / "room" / "comments" / "ch-01.json").read_text(encoding="utf-8")
    )
    assert on_disk[0]["status"] == "resolved"


def test_room_comment_patch_unknown_404(client):
    res = client.patch("/api/room/comments/1/c_nope", json={"status": "resolved"})
    assert res.status_code == 404


def test_room_comment_patch_invalid_status_422(client):
    created = client.post("/api/room/comments/1", json={"quote": "", "text": "q"}).json()
    res = client.patch(
        f"/api/room/comments/1/{created['id']}", json={"status": "not-a-status"}
    )
    assert res.status_code == 422


def test_room_comments_get_empty_chapter(client):
    res = client.get("/api/room/comments/9")
    assert res.status_code == 200
    assert res.json() == []


# ---------------------------------------------------------------------------
# missing ui extra
# ---------------------------------------------------------------------------


def test_create_app_still_raises_helpful_error_without_fastapi(
    project: WritingProject, monkeypatch: pytest.MonkeyPatch
):
    import builtins

    from stoner.ui.server import create_app

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fastapi" or name.startswith("fastapi."):
            raise ImportError("simulated missing fastapi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match=r"pip install 'st0n3r\[ui\]'"):
        create_app(project)


def test_index_html_contains_room_panel(client):
    res = client.get("/")
    assert res.status_code == 200
    assert 'data-view="room"' in res.text
    assert 'id="view-room"' in res.text
