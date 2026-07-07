"""CLI tests for the non-LLM commands (typer CliRunner, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.cli.main import app

runner = CliRunner()


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    result = runner.invoke(app, ["init", "mybook", "--path", str(tmp_path / "mybook")])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path / "mybook")
    return tmp_path / "mybook"


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "st0n3r" in result.output


def test_init_creates_structure(tmp_path: Path):
    result = runner.invoke(app, ["init", "novel", "--path", str(tmp_path / "novel")])
    assert result.exit_code == 0, result.output
    root = tmp_path / "novel"
    for rel in (
        "stoner.yaml",
        "canon/premise.md",
        "canon/style.md",
        "canon/threads.md",
        "outline/outline.md",
        "manuscript",
        ".stoner",
    ):
        assert (root / rel).exists(), rel


def test_init_refuses_existing(project_dir: Path):
    result = runner.invoke(app, ["init", "mybook", "--path", str(project_dir)])
    assert result.exit_code == 1


def test_status_and_threads(project_dir: Path):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "mybook" in result.output
    result = runner.invoke(app, ["threads"])
    assert result.exit_code == 0


def test_slop_on_chapter_and_save(project_dir: Path):
    sloppy = (
        "She couldn't help but delve into the tapestry of emotions, a testament "
        "to the myriad feelings that washed over her.\n"
    ) * 20
    (project_dir / "manuscript" / "ch-01.md").write_text(
        f"---\ntitle: One\nstatus: draft\n---\n\n{sloppy}", encoding="utf-8"
    )
    result = runner.invoke(app, ["slop", "1", "--fmt", "markdown", "--save"])
    assert result.exit_code == 0, result.output
    assert "delve" in result.output.lower() or "score" in result.output.lower()
    saved = list((project_dir / ".stoner" / "reviews").glob("slop-*.json"))
    assert saved


def test_slop_all_without_chapters_fails(project_dir: Path):
    result = runner.invoke(app, ["slop", "all"])
    assert result.exit_code == 1


def test_canon_list_show_search(project_dir: Path):
    result = runner.invoke(app, ["canon", "list"])
    assert result.exit_code == 0, result.output
    assert "premise" in result.output
    result = runner.invoke(app, ["canon", "show", "premise.md"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["canon", "search", "logline"])
    assert result.exit_code == 0


def test_canon_pack(project_dir: Path):
    result = runner.invoke(app, ["canon", "pack"])
    assert result.exit_code == 0


def test_ledger_and_providers(project_dir: Path):
    result = runner.invoke(app, ["ledger"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["providers"])
    assert result.exit_code == 0, result.output
    assert "anthropic" in result.output


def test_review_without_report_then_revise_fails_cleanly(project_dir: Path):
    (project_dir / "manuscript" / "ch-01.md").write_text(
        "---\ntitle: One\n---\n\nSome prose.", encoding="utf-8"
    )
    result = runner.invoke(app, ["revise", "1"])
    assert result.exit_code == 1
    assert "No review report" in result.output


def test_outside_project_fails_with_hint(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
