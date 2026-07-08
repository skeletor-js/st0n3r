"""Tests for the Writers' Room margin-comment store (U3): anchoring,
lifecycle, ledgering, and fail-soft file handling. No network anywhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.ledger import Ledger
from stoner.project import WritingProject
from stoner.room.comments import CommentStore

BODY = (
    "The harbor bell rang twice before Mara reached the quay.\n\n"
    "She counted the crates herself, twice, and the count came up short\n"
    "both times.\n"
)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "mybook", "My Book")
    scaffold_project(proj, "My Book")
    proj.write_chapter(1, {"title": "Arrival"}, BODY)
    return proj


def test_add_with_verbatim_quote_stores_span(project: WritingProject):
    store = CommentStore(project)
    c = store.add(1, quote="the count came up short", text="too flat?")
    assert c.span is not None
    assert BODY[c.span.start : c.span.end] == "the count came up short"
    assert c.span.line == 3
    assert c.status == "open"
    assert c.note == ""


def test_add_with_absent_quote_warns_and_stores_no_span(project: WritingProject):
    store = CommentStore(project)
    c = store.add(1, quote="this text is nowhere in the chapter", text="hm")
    assert c.span is None
    assert "not found" in c.note
    # still persisted and listed
    assert store.get(1, c.id) is not None


def test_add_without_quote_is_unanchored_without_warning_of_mismatch(project: WritingProject):
    c = CommentStore(project).add(1, quote="", text="general chapter note")
    assert c.span is None
    assert c.quote == ""


def test_append_response_flips_open_to_answered(project: WritingProject):
    store = CommentStore(project)
    c = store.add(1, quote="", text="why the bell?")
    updated = store.append_response(1, c.id, editor="line-editor", text="it tolls for thee", session="s1")
    assert updated.status == "answered"
    assert updated.responses[0].editor == "line-editor"
    assert updated.responses[0].session == "s1"
    # a second response accumulates without resetting status
    updated = store.append_response(1, c.id, editor="first-reader", text="agreed", session="s1")
    assert len(updated.responses) == 2
    assert updated.status == "answered"


def test_resolve_answered_comment_persists_and_ledgers(project: WritingProject):
    store = CommentStore(project)
    c = store.add(1, quote="", text="q")
    store.append_response(1, c.id, editor="room", text="a")
    updated = store.set_status(1, c.id, "resolved")
    assert updated.status == "resolved"
    on_disk = json.loads(
        (project.root / ".stoner" / "room" / "comments" / "ch-01.json").read_text(encoding="utf-8")
    )
    assert on_disk[0]["status"] == "resolved"
    actions = [e.action for e in Ledger(project.root).tail(10)]
    assert "room.comment.add" in actions
    assert "room.comment.answer" in actions
    assert "room.comment.resolve" in actions


def test_list_absent_chapter_returns_empty(project: WritingProject):
    assert CommentStore(project).list(7) == []


def test_list_only_open_filters(project: WritingProject):
    store = CommentStore(project)
    a = store.add(1, quote="", text="one")
    b = store.add(1, quote="", text="two")
    store.set_status(1, a.id, "dismissed")
    open_comments = store.list(1, only_open=True)
    assert [c.id for c in open_comments] == [b.id]
    assert len(store.list(1)) == 2


def test_malformed_comment_file_degrades_to_empty(project: WritingProject):
    path = project.root / ".stoner" / "room" / "comments" / "ch-01.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all", encoding="utf-8")
    store = CommentStore(project)
    assert store.list(1) == []
    # and the store recovers on the next write
    c = store.add(1, quote="", text="fresh start")
    assert [x.id for x in store.list(1)] == [c.id]


def test_set_status_unknown_comment_returns_none(project: WritingProject):
    assert CommentStore(project).set_status(1, "c_nope", "resolved") is None


def test_ledger_tail_shows_comment_add(project: WritingProject):
    CommentStore(project).add(1, quote="the count came up short", text="x")
    tail = Ledger(project.root).tail(1)
    assert tail[0].action == "room.comment.add"
    assert tail[0].detail["anchored"] is True


def test_comment_json_is_human_readable(project: WritingProject):
    CommentStore(project).add(1, quote="", text="note to the room")
    raw = (project.root / ".stoner" / "room" / "comments" / "ch-01.json").read_text(encoding="utf-8")
    assert "\n" in raw  # indented, git-diffable
    data = json.loads(raw)
    assert isinstance(data, list)
    assert data[0]["text"] == "note to the room"
