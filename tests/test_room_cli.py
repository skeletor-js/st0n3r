"""CLI tests for `stoner room ...` (U6). No network: `room session` is
exercised with `run_room_session` monkeypatched to a canned result (typer
CliRunner, tests/test_pacing_cli.py pattern)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.cli.main import app
from stoner.room.session import RoomSessionResult
from stoner.types import Usage

runner = CliRunner()

BODY = (
    "The harbor bell rang twice before Mara reached the quay. She counted\n"
    "the crates herself, twice, and the count came up short both times.\n"
)


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    result = runner.invoke(app, ["init", "mybook", "--path", str(tmp_path / "mybook")])
    assert result.exit_code == 0, result.output
    root = tmp_path / "mybook"
    (root / "manuscript" / "ch-01.md").write_text(
        f"---\ntitle: One\n---\n\n{BODY}", encoding="utf-8"
    )
    monkeypatch.chdir(root)
    return root


def test_room_help_lists_all_five_commands(project_dir: Path):
    result = runner.invoke(app, ["room", "--help"])
    assert result.exit_code == 0, result.output
    for cmd in ("session", "comment", "comments", "notebook", "status"):
        assert cmd in result.output


def test_room_comment_then_comments_shows_it(project_dir: Path):
    result = runner.invoke(
        app,
        ["room", "comment", "1", "--quote", "the count came up short", "--text", "too flat?"],
    )
    assert result.exit_code == 0, result.output
    assert "comment" in result.output and "added" in result.output

    result = runner.invoke(app, ["room", "comments", "1"])
    assert result.exit_code == 0, result.output
    assert "too flat?" in result.output
    assert "open" in result.output


def test_room_comment_unanchored_warns(project_dir: Path):
    result = runner.invoke(
        app, ["room", "comment", "1", "--quote", "text nowhere in chapter", "--text", "hm"]
    )
    assert result.exit_code == 0, result.output
    assert "not found" in result.output


def test_room_comments_resolve_round_trip(project_dir: Path):
    runner.invoke(app, ["room", "comment", "1", "--text", "note"])
    result = runner.invoke(app, ["room", "comments", "1"])
    comment_id = next(
        word for word in result.output.split() if word.startswith("c_")
    )
    result = runner.invoke(app, ["room", "comments", "1", "--resolve", comment_id])
    assert result.exit_code == 0, result.output
    assert "resolved" in result.output
    # resolved comments hide by default, show with --all
    result = runner.invoke(app, ["room", "comments", "1"])
    assert "no comments" in result.output
    result = runner.invoke(app, ["room", "comments", "1", "--all"])
    assert "note" in result.output


def test_room_comments_resolve_unknown_id_fails(project_dir: Path):
    result = runner.invoke(app, ["room", "comments", "1", "--resolve", "c_nope"])
    assert result.exit_code == 1


def test_room_notebook_fresh_project_prints_empty_message(project_dir: Path):
    result = runner.invoke(app, ["room", "notebook", "line-editor"])
    assert result.exit_code == 0, result.output
    assert "empty" in result.output


def test_room_notebook_unknown_editor_exits_1(project_dir: Path):
    result = runner.invoke(app, ["room", "notebook", "nonexistent-editor"])
    assert result.exit_code == 1
    assert "roster" in result.output


def test_room_notebook_accepts_editor_name_or_slug(project_dir: Path):
    from stoner.project import WritingProject
    from stoner.room.notebook import Notebook

    project = WritingProject(project_dir)
    nb = Notebook(project, "line-editor", project.config.room)
    nb.set_opinion("prose is tightening")
    nb.upsert_item("f_1", 1, "q", "an open issue", "minor", "line", "s1")
    result = runner.invoke(app, ["room", "notebook", "Line Editor"])
    assert result.exit_code == 0, result.output
    assert "prose is tightening" in result.output
    assert "an open issue" in result.output


def test_room_session_prints_table_and_exits_0_even_with_unmet_obligations(
    project_dir: Path, monkeypatch
):
    canned = RoomSessionResult(
        session_id="ch-01-123",
        scope="chapter",
        chapter=1,
        editors=["dev-editor", "line-editor"],
        findings_count=3,
        findings_by_editor={"dev-editor": 2, "line-editor": 1},
        agreements=1,
        disagreements=2,
        relocated={"persisting": 1},
        obligations_unmet=["c_abc123"],
        usage=Usage(input_tokens=100, output_tokens=50),
        notes=["a session note"],
        json_path=str(project_dir / ".stoner" / "room" / "sessions" / "ch-01-123.json"),
        md_path=str(project_dir / ".stoner" / "room" / "sessions" / "ch-01-123.md"),
    )
    calls = {}

    def fake_run(project, chapter=None, book=False, model=None, provider=None):
        calls["chapter"] = chapter
        calls["book"] = book
        return canned

    import stoner.room.session as session_mod

    monkeypatch.setattr(session_mod, "run_room_session", fake_run)
    result = runner.invoke(app, ["room", "session", "1"])
    assert result.exit_code == 0, result.output  # advisory: exit 0 despite unmet
    assert calls == {"chapter": 1, "book": False}
    assert "dev-editor" in result.output
    assert "disagreements: 2" in result.output
    assert "warning" in result.output and "c_abc123" in result.output
    assert "a session note" in result.output


def test_room_session_requires_chapter_or_book(project_dir: Path):
    result = runner.invoke(app, ["room", "session"])
    assert result.exit_code == 1
    assert "--book" in result.output


def test_room_session_book_flag_passes_through(project_dir: Path, monkeypatch):
    canned = RoomSessionResult(session_id="book-1", scope="book", editors=[])
    seen = {}

    def fake_run(project, chapter=None, book=False, model=None, provider=None):
        seen["book"] = book
        return canned

    import stoner.room.session as session_mod

    monkeypatch.setattr(session_mod, "run_room_session", fake_run)
    result = runner.invoke(app, ["room", "session", "--book"])
    assert result.exit_code == 0, result.output
    assert seen["book"] is True


def test_room_commands_outside_project_exit_1(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for args in (["room", "session", "1"], ["room", "comments", "1"], ["room", "status"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 1, args
        assert "stoner init" in result.output


def test_room_status_shows_open_comments_and_persisting_flags(project_dir: Path):
    from stoner.project import WritingProject
    from stoner.room.notebook import Notebook

    runner.invoke(app, ["room", "comment", "1", "--text", "open question"])
    project = WritingProject(project_dir)
    nb = Notebook(project, "continuity-pedant", project.config.room)
    nb.upsert_item("f_1", 1, "q", "a persisting problem", "minor", "continuity", "s1")
    nb.mark_persisting("f_1", "s2")

    result = runner.invoke(app, ["room", "status"])
    assert result.exit_code == 0, result.output
    assert "open comments: 1" in result.output
    assert "open question" in result.output
    assert "continuity-pedant" in result.output
    assert "a persisting problem" in result.output
