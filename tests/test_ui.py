"""Tests for the local web UI (src/stoner/ui): FastAPI app + JSON API."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.project import WritingProject
from stoner.types import Finding, ReviewReport, Severity, SlopReport, Span

SLOPPY_BODY = (
    "Sarah couldn't help but delve into the tapestry of memories that haunted her.\n"
    "It was a testament to her unwavering resolve. Little did she know, the\n"
    "maelstrom of emotions would only grow stronger with each passing heartbeat.\n"
)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "mybook", "My Book")
    scaffold_project(proj, "My Book")
    proj.write_chapter(1, {"title": "Arrival", "status": "draft", "pov": "Aria"}, SLOPPY_BODY)
    return proj


@pytest.fixture()
def client(project: WritingProject):
    from fastapi.testclient import TestClient

    from stoner.ui.server import create_app

    app = create_app(project)
    return TestClient(app)


def _write_review_report(project: WritingProject, filename: str, findings: list[Finding], **overrides) -> Path:
    report = ReviewReport(
        path="manuscript/ch-01.md",
        passes=["continuity"],
        findings=findings,
        summary="looks fine",
        model="anthropic/claude-sonnet-5",
        **overrides,
    )
    dest = project.root / ".stoner" / "reviews" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return dest


def _write_slop_report(project: WritingProject, filename: str) -> Path:
    report = SlopReport(
        path="manuscript/ch-01.md",
        score=62.0,
        subscores={"lexicon": 80.0},
        findings=[
            Finding(
                source="slop:lexicon",
                severity=Severity.minor,
                category="lexicon",
                span=Span(start=0, end=5, line=1),
                quote="delve",
                issue="overused word: delve",
            )
        ],
    )
    dest = project.root / ".stoner" / "reviews" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# create_app / missing dependency
# ---------------------------------------------------------------------------


def test_create_app_raises_helpful_error_without_fastapi(project: WritingProject, monkeypatch: pytest.MonkeyPatch):
    from stoner.ui.server import create_app

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fastapi" or name.startswith("fastapi."):
            raise ImportError("simulated missing fastapi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match=r"pip install 'st0n3r\[ui\]'"):
        create_app(project)


# ---------------------------------------------------------------------------
# status / chapters
# ---------------------------------------------------------------------------


def test_status_endpoint(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "My Book"
    assert data["chapters"] == 1


def test_index_serves_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "st0n3r" in res.text


def test_chapters_list(client):
    res = client.get("/api/chapters")
    assert res.status_code == 200
    chapters = res.json()
    assert len(chapters) == 1
    assert chapters[0] == {
        "number": 1,
        "title": "Arrival",
        "status": "draft",
        "pov": "Aria",
        "words": chapters[0]["words"],
    }
    assert chapters[0]["words"] > 0


def test_chapter_detail(client):
    res = client.get("/api/chapters/1")
    assert res.status_code == 200
    data = res.json()
    assert data["frontmatter"]["title"] == "Arrival"
    assert "delve" in data["body"]


def test_chapter_detail_404(client):
    res = client.get("/api/chapters/99")
    assert res.status_code == 404
    assert "ch-99" in res.json()["detail"]


def test_chapter_slop_live(client):
    res = client.get("/api/chapters/1/slop")
    assert res.status_code == 200
    data = res.json()
    assert data["score"] > 0
    assert data["findings"], "expected findings for sloppy chapter text"
    for finding in data["findings"]:
        span = finding.get("span")
        if span is None:
            continue
        assert 0 <= span["start"] < span["end"]


def test_chapter_slop_spans_align_with_returned_body(client):
    body = client.get("/api/chapters/1").json()["body"]
    report = client.get("/api/chapters/1/slop").json()
    matched_any = False
    for finding in report["findings"]:
        span = finding.get("span")
        if span is None:
            continue
        assert body[span["start"] : span["end"]] == finding["quote"]
        matched_any = True
    assert matched_any


def test_chapter_slop_404_for_missing_chapter(client):
    res = client.get("/api/chapters/42/slop")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# canon
# ---------------------------------------------------------------------------


def test_canon_list(client):
    res = client.get("/api/canon")
    assert res.status_code == 200
    entries = res.json()
    kinds = {e["kind"] for e in entries}
    assert "premise" in kinds
    assert "style" in kinds
    assert all({"rel_path", "kind", "name"} <= set(e) for e in entries)


def test_canon_entry(client):
    res = client.get("/api/canon/entry", params={"path": "canon/premise.md"})
    assert res.status_code == 200
    data = res.json()
    assert "My Book" in data["body"]


def test_canon_entry_missing_404(client):
    res = client.get("/api/canon/entry", params={"path": "canon/does-not-exist.md"})
    assert res.status_code == 404


def test_canon_entry_rejects_parent_traversal(client):
    res = client.get("/api/canon/entry", params={"path": "../stoner.yaml"})
    assert res.status_code == 400


def test_canon_entry_rejects_absolute_path(client, tmp_path):
    res = client.get("/api/canon/entry", params={"path": "/etc/passwd"})
    assert res.status_code == 400


def test_canon_entry_rejects_escape_via_nested_traversal(client):
    res = client.get("/api/canon/entry", params={"path": "canon/../../stoner.yaml"})
    assert res.status_code == 400


def test_threads_endpoint(client, project: WritingProject):
    store = CanonStore(project)
    store.add_thread("t1", "who killed the duke", opened_in="ch-01")
    res = client.get("/api/threads")
    assert res.status_code == 200
    rows = res.json()
    assert rows[0]["id"] == "t1"
    assert rows[0]["thread"] == "who killed the duke"


# ---------------------------------------------------------------------------
# reviews
# ---------------------------------------------------------------------------


def test_reviews_list_empty(client):
    res = client.get("/api/reviews")
    assert res.status_code == 200
    assert res.json() == []


def test_reviews_list_and_detail(client, project: WritingProject):
    _write_slop_report(project, "ch-01-slop.json")
    finding = Finding(source="review:continuity", severity=Severity.major, category="continuity", issue="eye color changed")
    _write_review_report(project, "ch-01-review.json", [finding])

    res = client.get("/api/reviews")
    assert res.status_code == 200
    listing = {r["file"]: r for r in res.json()}
    assert listing["ch-01-slop.json"]["kind"] == "slop"
    assert listing["ch-01-slop.json"]["chapter"] == 1
    assert listing["ch-01-review.json"]["kind"] == "review"

    res = client.get("/api/reviews/ch-01-review.json")
    assert res.status_code == 200
    data = res.json()
    assert data["findings"][0]["issue"] == "eye color changed"


def test_review_detail_404(client):
    res = client.get("/api/reviews/does-not-exist.json")
    assert res.status_code == 404


@pytest.mark.parametrize("bad_file", ["../stoner.yaml", "/etc/passwd", "..", "sub/dir.json"])
def test_review_detail_rejects_traversal(client, bad_file):
    res = client.get(f"/api/reviews/{bad_file}")
    assert res.status_code in (400, 404)  # some are blocked by routing itself (404), rest by the jail (400)


def test_review_finding_patch_round_trip(client, project: WritingProject):
    finding = Finding(id="f_abc123", source="review:continuity", severity=Severity.major, issue="eye color changed")
    _write_review_report(project, "ch-01-review.json", [finding])

    res = client.patch("/api/reviews/ch-01-review.json/findings/f_abc123", json={"status": "accepted"})
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"

    # persisted on disk
    on_disk = json.loads((project.root / ".stoner" / "reviews" / "ch-01-review.json").read_text())
    assert on_disk["findings"][0]["status"] == "accepted"

    # re-fetch via the API too
    res = client.get("/api/reviews/ch-01-review.json")
    assert res.json()["findings"][0]["status"] == "accepted"


def test_review_finding_patch_unknown_finding_404(client, project: WritingProject):
    finding = Finding(id="f_real", source="review:continuity", severity=Severity.major, issue="x")
    _write_review_report(project, "ch-01-review.json", [finding])
    res = client.patch("/api/reviews/ch-01-review.json/findings/does-not-exist", json={"status": "accepted"})
    assert res.status_code == 404


def test_review_finding_patch_invalid_status_422(client, project: WritingProject):
    finding = Finding(id="f_real", source="review:continuity", severity=Severity.major, issue="x")
    _write_review_report(project, "ch-01-review.json", [finding])
    res = client.patch("/api/reviews/ch-01-review.json/findings/f_real", json={"status": "not-a-status"})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------


def test_ledger_endpoint(client, project: WritingProject):
    from stoner.ledger import Ledger

    ledger = Ledger(project.root)
    ledger.append("write.draft", target="manuscript/ch-01.md")
    ledger.append("slop.check", target="manuscript/ch-01.md")

    res = client.get("/api/ledger")
    assert res.status_code == 200
    entries = res.json()
    assert len(entries) == 2
    assert entries[-1]["action"] == "slop.check"


def test_ledger_endpoint_respects_n(client, project: WritingProject):
    from stoner.ledger import Ledger

    ledger = Ledger(project.root)
    for i in range(5):
        ledger.append("write.draft", target=f"manuscript/ch-{i:02d}.md")

    res = client.get("/api/ledger", params={"n": 2})
    assert res.status_code == 200
    assert len(res.json()) == 2


def test_ledger_empty(client):
    res = client.get("/api/ledger")
    assert res.status_code == 200
    assert res.json() == []


def test_reviews_and_ledger_reflect_external_edits(client, project: WritingProject):
    """Endpoints read from disk fresh each request -- no caching."""
    assert client.get("/api/reviews").json() == []
    _write_slop_report(project, "late.json")
    assert len(client.get("/api/reviews").json()) == 1

    assert client.get("/api/ledger").json() == []
    from stoner.ledger import Ledger

    Ledger(project.root).append("write.draft", target="manuscript/ch-01.md")
    assert len(client.get("/api/ledger").json()) == 1


# ---------------------------------------------------------------------------
# book (autonomous run state)
# ---------------------------------------------------------------------------


def test_book_endpoint_absent_returns_empty_object(client):
    """No .stoner/book-state.json yet -- never a 404, just {}."""
    res = client.get("/api/book")
    assert res.status_code == 200
    assert res.json() == {}


def test_book_endpoint_returns_state_when_present(client, project: WritingProject):
    state = {
        "chapters_planned": 20,
        "chapters_done": {
            "1": {"words": 2400, "slop": 12.0, "reviewed": True, "revision_cycles": 1},
            "2": {"words": 2100, "slop": 30.5, "reviewed": False, "revision_cycles": 0},
        },
        "current": 3,
        "phase": "drafting",
        "book_reviews": [{"at_chapter": 2, "majors": 1, "created_at": 1700000000.0}],
        "updated_at": 1700000100.0,
    }
    path = project.root / ".stoner" / "book-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")

    res = client.get("/api/book")
    assert res.status_code == 200
    assert res.json() == state


def test_book_endpoint_corrupt_json_returns_empty_object(client, project: WritingProject):
    path = project.root / ".stoner" / "book-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    res = client.get("/api/book")
    assert res.status_code == 200
    assert res.json() == {}


def test_book_endpoint_non_object_json_returns_empty_object(client, project: WritingProject):
    path = project.root / ".stoner" / "book-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")

    res = client.get("/api/book")
    assert res.status_code == 200
    assert res.json() == {}


# ---------------------------------------------------------------------------
# pacing (latest saved pacing report)
# ---------------------------------------------------------------------------


def _write_pacing_report(project: WritingProject, filename: str, **overrides) -> Path:
    payload = {
        "kind": "pacing",
        "created_at": 1700000000.0,
        "llm": False,
        "model": "",
        "series": [
            {
                "chapter": 1,
                "words": 1200,
                "dialogue_ratio": 0.4,
                "interiority_ratio": 0.2,
                "action_ratio": 0.4,
                "in_scene_fraction": 0.8,
                "pov": "Aria",
                "ending_shape": "one_line_punch",
                "beat_sheet": "present",
                "tension": "skipped",
                "changes_hands": None,
                "beats": None,
            }
        ],
        "flatlines": [],
        "stats": {},
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "findings": [],
    }
    payload.update(overrides)
    dest = project.root / ".stoner" / "reviews" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload), encoding="utf-8")
    return dest


def test_pacing_endpoint_absent_returns_empty_object(client):
    res = client.get("/api/pacing")
    assert res.status_code == 200
    assert res.json() == {}


def test_pacing_endpoint_returns_latest_report(client, project: WritingProject):
    _write_pacing_report(project, "pacing-1000.json", created_at=1000.0)
    _write_pacing_report(project, "pacing-2000.json", created_at=2000.0)

    res = client.get("/api/pacing")
    assert res.status_code == 200
    data = res.json()
    assert data["created_at"] == 2000.0
    assert data["kind"] == "pacing"
    assert data["series"][0]["chapter"] == 1


def test_pacing_endpoint_malformed_json_returns_empty_object(client, project: WritingProject):
    dest = project.root / ".stoner" / "reviews" / "pacing-9999.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("{not valid json", encoding="utf-8")

    res = client.get("/api/pacing")
    assert res.status_code == 200
    assert res.json() == {}


def test_reviews_listing_labels_pacing_kind_and_keeps_existing_kinds(client, project: WritingProject):
    _write_slop_report(project, "ch-01-slop.json")
    finding = Finding(source="review:continuity", severity=Severity.major, issue="x")
    _write_review_report(project, "ch-01-review.json", [finding])
    _write_pacing_report(project, "pacing-1234.json")

    res = client.get("/api/reviews")
    assert res.status_code == 200
    listing = {r["file"]: r for r in res.json()}
    assert listing["pacing-1234.json"]["kind"] == "pacing"
    assert listing["ch-01-slop.json"]["kind"] == "slop"  # regression
    assert listing["ch-01-review.json"]["kind"] == "review"  # regression
