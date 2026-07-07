"""CLI tests for `stoner drafts` (typer CliRunner, no network).

Everything here is deterministic: list/show/diff/blame/restore/snapshot/
prune never touch a provider. Refactor commands are covered in
`tests/test_archaeology_refactor.py`; this file covers the writer-facing
snapshot surface (plan 009 U4).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.archaeology.snapshots import snapshot_write_chapter
from stoner.cli.main import app
from stoner.ledger import Ledger
from stoner.project import WritingProject

runner = CliRunner()

BODY_A = (
    "He walked to the window and stood there a while. The yard was bare.\n\n"
    "A dog crossed the road and stopped, looked back, and went on.\n\n"
    "The clock in the hall kept its slow count.\n"
)
BODY_B = (
    "He walked to the window and stood there a while. The yard was bare.\n\n"
    "A dog crossed the road and stopped, looked back, and went on again.\n\n"
    "The clock in the hall kept its slow count.\n"
)
BODY_C = (
    "In the morning there would be work: the fence along the north line.\n\n"
    "The kettle sat cold on the stove.\n"
)


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "testbook")
    monkeypatch.chdir(p.root)
    return p


@pytest.fixture()
def seeded(project: WritingProject) -> WritingProject:
    """A chapter with two snapshots: A displaced by B, B displaced by C."""
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_A, reason="draft")
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_B, reason="slop-revise")
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_C, reason="review-revise")
    return project


def entries_of(project: WritingProject, number: int) -> list[dict]:
    manifest = json.loads(
        (project.root / ".stoner" / "drafts" / f"ch-{number:02d}" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    return manifest["entries"]


def test_drafts_help_lists_all_commands():
    result = runner.invoke(app, ["drafts", "--help"])
    assert result.exit_code == 0
    for cmd in ("list", "show", "diff", "blame", "restore", "snapshot", "prune", "verify", "refactor"):
        assert cmd in result.output
    result = runner.invoke(app, ["drafts", "refactor", "--help"])
    assert result.exit_code == 0
    for cmd in ("merge", "split", "move-reveal", "flip-pov"):
        assert cmd in result.output


def test_list_show_diff_over_seeded_chapter(seeded):
    result = runner.invoke(app, ["drafts", "list", "1"])
    assert result.exit_code == 0, result.output
    assert "draft" in result.output and "slop-revise" in result.output

    result = runner.invoke(app, ["drafts", "show", "1", "1"])
    assert result.exit_code == 0, result.output
    assert "He walked to the window" in result.output
    assert result.output.startswith("---")  # frontmatter included

    # diff between two snapshots
    result = runner.invoke(app, ["drafts", "diff", "1", "1", "2"])
    assert result.exit_code == 0, result.output
    assert "-A dog crossed the road and stopped, looked back, and went on." in result.output
    assert "+A dog crossed the road and stopped, looked back, and went on again." in result.output

    # diff between a snapshot and the current body
    result = runner.invoke(app, ["drafts", "diff", "1", "2"])
    assert result.exit_code == 0, result.output
    assert "+The kettle sat cold on the stove." in result.output


def test_blame_renders_and_filters(seeded):
    result = runner.invoke(app, ["drafts", "blame", "1"])
    assert result.exit_code == 0, result.output
    assert "review-revise" in result.output

    result = runner.invoke(app, ["drafts", "blame", "1", "--quote", "kettle"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["drafts", "blame", "1", "--quote", "no such words here"])
    assert result.exit_code == 1


def test_whole_chapter_restore(seeded):
    result = runner.invoke(app, ["drafts", "restore", "1", "1"])
    assert result.exit_code == 0, result.output

    _fm, body = seeded.read_chapter(1)
    assert body.strip() == BODY_A.strip()
    entries = entries_of(seeded, 1)
    assert entries[-1]["reason"] == "restore"
    assert any(
        e.action == "drafts.restore" and e.detail.get("chapter") == 1
        for e in Ledger(seeded.root).tail(20)
    )


def test_paragraph_restore_replaces_exactly_one_paragraph(seeded):
    # Current body is C; bring back paragraph 2 of snapshot 2 (body B's
    # dog paragraph) at an explicit position.
    result = runner.invoke(
        app, ["drafts", "restore", "1", "2", "--paragraph", "2", "--at", "2"]
    )
    assert result.exit_code == 0, result.output
    _fm, body = seeded.read_chapter(1)
    paragraphs = [p for p in body.split("\n\n") if p.strip()]
    assert paragraphs[0].startswith("In the morning")
    assert paragraphs[1].startswith("A dog crossed the road")


def test_paragraph_restore_aligns_deterministically(project):
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_A, reason="draft")
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_B, reason="draft")
    # Restore paragraph 2 of the snapshot (the "went on." variant): aligns
    # to the current "went on again." paragraph without --at.
    result = runner.invoke(app, ["drafts", "restore", "1", "1", "--paragraph", "2"])
    assert result.exit_code == 0, result.output
    _fm, body = project.read_chapter(1)
    assert "went on.\n" in body or body.rstrip().endswith("slow count.")
    assert "went on again" not in body


def test_paragraph_restore_ambiguous_without_at_fails(project):
    ambiguous = "The bell rang once in the tower.\n\nThe bell rang once in the tower.\n"
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_A, reason="draft")
    snapshot_write_chapter(project, 1, {"title": "One"}, ambiguous, reason="draft")
    before = (project.root / "manuscript" / "ch-01.md").read_text(encoding="utf-8")

    result = runner.invoke(app, ["drafts", "restore", "1", "1", "--paragraph", "1"])
    assert result.exit_code == 1
    assert "--at" in result.output + str(result.exception or "")
    after = (project.root / "manuscript" / "ch-01.md").read_text(encoding="utf-8")
    assert after == before  # nothing touched


def test_restore_refuses_hand_edit_without_force(seeded):
    fm, _ = seeded.read_chapter(1)
    seeded.write_chapter(1, fm, "A hand-written paragraph the harness never saw.\n")
    before = (seeded.root / "manuscript" / "ch-01.md").read_text(encoding="utf-8")

    result = runner.invoke(app, ["drafts", "restore", "1", "1"])
    assert result.exit_code == 1
    assert (seeded.root / "manuscript" / "ch-01.md").read_text(encoding="utf-8") == before

    result = runner.invoke(app, ["drafts", "restore", "1", "1", "--force"])
    assert result.exit_code == 0, result.output
    _fm, body = seeded.read_chapter(1)
    assert body.strip() == BODY_A.strip()
    # The hand-edit survived as a human-edit snapshot.
    entries = entries_of(seeded, 1)
    human = [e for e in entries if e["reason"] == "human-edit"]
    assert human
    snap_dir = seeded.root / ".stoner" / "drafts" / "ch-01"
    assert "hand-written paragraph" in (snap_dir / human[0]["file"]).read_text(encoding="utf-8")


def test_manual_snapshot_command(seeded):
    fm, _ = seeded.read_chapter(1)
    seeded.write_chapter(1, fm, "Hand edit to preserve.\n")
    result = runner.invoke(app, ["drafts", "snapshot", "1"])
    assert result.exit_code == 0, result.output
    entries = entries_of(seeded, 1)
    assert entries[-1]["reason"] == "manual"
    snap_dir = seeded.root / ".stoner" / "drafts" / "ch-01"
    assert "Hand edit to preserve." in (snap_dir / entries[-1]["file"]).read_text(encoding="utf-8")
    assert any(
        e.action == "drafts.snapshot" and e.detail.get("reason") == "manual"
        for e in Ledger(seeded.root).tail(10)
    )


@pytest.fixture()
def five_snapshots(project: WritingProject) -> WritingProject:
    """Five manifest entries including one human-edit in the middle."""
    bodies = [
        "First body, the original draft. It stays forever.\n",
        "Second body, an early revision pass.\n",
        "Third body, another revision pass.\n",
    ]
    for body in bodies:
        snapshot_write_chapter(project, 1, {"title": "One"}, body, reason="draft")
    fm, _ = project.read_chapter(1)
    project.write_chapter(1, fm, "A hand-edit between harness runs.\n")
    snapshot_write_chapter(project, 1, {"title": "One"}, "Fourth body after the hand edit.\n", reason="draft")
    snapshot_write_chapter(project, 1, {"title": "One"}, "Fifth body, the newest.\n", reason="draft")
    assert len(entries_of(project, 1)) == 5
    return project


def test_prune_keeps_first_human_edit_and_last_two(five_snapshots):
    project = five_snapshots
    result = runner.invoke(app, ["drafts", "prune", "1", "--keep", "2", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert all(not e["pruned"] for e in entries_of(project, 1))  # dry run touched nothing
    snap_dir = project.root / ".stoner" / "drafts" / "ch-01"
    files_before = sorted(p.name for p in snap_dir.glob("*.md"))

    result = runner.invoke(app, ["drafts", "prune", "1", "--keep", "2"])
    assert result.exit_code == 0, result.output

    entries = entries_of(project, 1)
    by_seq = {e["seq"]: e for e in entries}
    assert not by_seq[1]["pruned"]  # original draft
    assert not by_seq[3]["pruned"]  # human-edit
    assert by_seq[3]["reason"] == "human-edit"
    assert not by_seq[4]["pruned"] and not by_seq[5]["pruned"]  # last two
    assert by_seq[2]["pruned"]
    assert by_seq[2]["sha256"]  # hash retained for the attribution chain

    files_after = sorted(p.name for p in snap_dir.glob("*.md"))
    assert by_seq[2]["file"] not in files_after
    assert by_seq[1]["file"] in files_after
    assert by_seq[3]["file"] in files_after
    assert set(files_after) < set(files_before)
    assert any(e.action == "drafts.prune" for e in Ledger(project.root).tail(10))


def test_prune_without_keep_or_config_fails(seeded):
    result = runner.invoke(app, ["drafts", "prune", "1"])
    assert result.exit_code == 1


def test_error_paths_unknown_chapter_and_seq(seeded):
    result = runner.invoke(app, ["drafts", "show", "9", "1"])
    assert result.exit_code == 1
    result = runner.invoke(app, ["drafts", "show", "1", "99"])
    assert result.exit_code == 1
    result = runner.invoke(app, ["drafts", "restore", "1", "99"])
    assert result.exit_code == 1
    result = runner.invoke(app, ["drafts", "list", "9"])
    assert result.exit_code == 0  # informative, not an error
    assert "no snapshots" in result.output
