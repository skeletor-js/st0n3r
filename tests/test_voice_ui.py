"""Tests for the voice endpoints of the local web UI (src/stoner/ui).

Mirrors tests/test_ui.py: TestClient over `create_app`, no network. Fixture
prose comes from tests/test_voice.py's deterministic generators (voice A =
long-breath fingerprint, voice B = clipped chapter, so drift findings fire).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_voice import clipped_text, long_breath_text

from stoner.canon.scaffold import scaffold_project
from stoner.project import WritingProject
from stoner.voice.fingerprint import learn_fingerprint, save_fingerprint


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "mybook", "My Book")
    scaffold_project(proj, "My Book")
    proj.write_chapter(
        1, {"title": "Arrival", "status": "draft", "pov": "Aria"}, clipped_text(9, 24)
    )
    return proj


@pytest.fixture()
def client(project: WritingProject):
    from fastapi.testclient import TestClient

    from stoner.ui.server import create_app

    return TestClient(create_app(project))


def _learn(project: WritingProject) -> None:
    save_fingerprint(project, learn_fingerprint([("a.md", long_breath_text(1, 60))]))


# ---------------------------------------------------------------------------
# /api/voice (fingerprint meta)
# ---------------------------------------------------------------------------


def test_voice_meta_404_without_fingerprint(client):
    res = client.get("/api/voice")
    assert res.status_code == 404
    assert "voice learn" in res.json()["detail"]


def test_voice_meta_with_fingerprint(client, project: WritingProject):
    _learn(project)
    res = client.get("/api/voice")
    assert res.status_code == 200
    data = res.json()
    assert data["version"] == 1
    assert data["exemplars"] == ["a.md"]
    assert data["segment_count"] >= 5
    assert data["total_words"] >= 5000
    assert data["thin"] is False


# ---------------------------------------------------------------------------
# /api/chapters/{n}/voice
# ---------------------------------------------------------------------------


def test_chapter_voice_404_without_fingerprint(client):
    res = client.get("/api/chapters/1/voice")
    assert res.status_code == 404
    assert "voice learn" in res.json()["detail"]


def test_chapter_voice_404_for_missing_chapter(client, project: WritingProject):
    _learn(project)
    res = client.get("/api/chapters/42/voice")
    assert res.status_code == 404
    assert "ch-42" in res.json()["detail"]


def test_chapter_voice_live(client, project: WritingProject):
    _learn(project)
    res = client.get("/api/chapters/1/voice")
    assert res.status_code == 200
    data = res.json()
    assert data["kind"] == "voice"
    assert data["score"] >= 30.0  # clipped chapter vs long-breath fingerprint
    assert data["subscores"]
    assert data["findings"], "expected drifting-window findings"
    assert data["stats"]["verdict"] in {"off voice", "broke voice"}


def test_chapter_voice_spans_align_with_returned_body(client, project: WritingProject):
    _learn(project)
    body = client.get("/api/chapters/1").json()["body"]
    report = client.get("/api/chapters/1/voice").json()
    matched_any = False
    for finding in report["findings"]:
        span = finding.get("span")
        if span is None:
            continue
        assert 0 <= span["start"] < span["end"] <= len(body)
        assert body[span["start"] : span["end"]] == finding["quote"]
        assert body.count("\n", 0, span["start"]) + 1 == span["line"]
        matched_any = True
    assert matched_any


# ---------------------------------------------------------------------------
# review listing: saved voice reports are kind "voice"
# ---------------------------------------------------------------------------


def test_saved_voice_report_lists_as_voice_kind(client, project: WritingProject):
    from stoner.types import VoiceReport

    report = VoiceReport(path="manuscript/ch-01.md", score=42.0, subscores={"rhythm": 50.0})
    dest = project.root / ".stoner" / "reviews" / "voice-ch-01-1700000000.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    res = client.get("/api/reviews")
    assert res.status_code == 200
    listing = {r["file"]: r for r in res.json()}
    entry = listing["voice-ch-01-1700000000.json"]
    assert entry["kind"] == "voice"
    assert entry["chapter"] == 1

    detail = client.get("/api/reviews/voice-ch-01-1700000000.json")
    assert detail.status_code == 200
    assert json.loads(detail.text)["kind"] == "voice"
