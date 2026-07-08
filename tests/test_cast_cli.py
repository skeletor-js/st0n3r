"""CLI + config tests for the cast subsystem (typer CliRunner, no network).

Model-calling commands (`update`, `check`, `scene`) are exercised with the
pipeline functions monkeypatched, mirroring `tests/test_cli.py`'s no-network
posture. The `run_write` hook is deferred to the integration wiring stage and
is not tested here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import stoner.interiority as interiority
from stoner.cli.main import app
from stoner.config import CastConfig, StonerConfig
from stoner.interiority import (
    CastApplyResult,
    CastConflict,
    CastReport,
    CastSheet,
    CastStore,
    SceneResult,
)
from stoner.ledger import Ledger
from stoner.project import WritingProject

runner = CliRunner()


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    result = runner.invoke(app, ["init", "mybook", "--path", str(tmp_path / "mybook")])
    assert result.exit_code == 0, result.output
    root = tmp_path / "mybook"
    # a canon character to seed a cast sheet from
    (root / "canon" / "characters" / "ruth-vann.md").write_text(
        "---\nname: Ruth Vann\nrole: protagonist\n---\n\n## Voice\n\nClipped.\n\n"
        "## Wants / Fears\n\nStated: keep the house. Real: be forgiven.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    return root


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_cast_config_defaults():
    cfg = CastConfig()
    assert cfg.auto_update is True
    assert cfg.scene_max_rounds == 8
    assert cfg.scene_mode == "auto"


def test_cast_config_roundtrips_through_yaml(tmp_path: Path):
    (tmp_path / "stoner.yaml").write_text(
        "project_name: rt\ncast:\n  auto_update: false\n  scene_max_rounds: 3\n  scene_mode: single\n",
        encoding="utf-8",
    )
    cfg = StonerConfig.load(tmp_path)
    assert cfg.cast.auto_update is False
    assert cfg.cast.scene_max_rounds == 3
    assert cfg.cast.scene_mode == "single"


def test_cast_config_defaults_when_absent(tmp_path: Path):
    (tmp_path / "stoner.yaml").write_text("project_name: rt\n", encoding="utf-8")
    cfg = StonerConfig.load(tmp_path)
    assert cfg.cast.auto_update is True
    assert cfg.cast.scene_max_rounds == 8


# ---------------------------------------------------------------------------
# cast group surface
# ---------------------------------------------------------------------------


def test_cast_help_lists_six_commands(project_dir: Path):
    result = runner.invoke(app, ["cast", "--help"])
    assert result.exit_code == 0
    for cmd in ("init", "list", "show", "update", "check", "scene"):
        assert cmd in result.output


def test_cast_init_creates_sheet_and_ledgers(project_dir: Path):
    result = runner.invoke(app, ["cast", "init", "Ruth Vann"])
    assert result.exit_code == 0, result.output
    assert (project_dir / ".stoner" / "cast" / "ruth-vann.json").exists()
    actions = [e.action for e in Ledger(project_dir).tail(10)]
    assert "cast.init" in actions
    # second run refuses
    again = runner.invoke(app, ["cast", "init", "Ruth Vann"])
    assert again.exit_code == 1


def test_cast_list_and_show_populated_and_empty(project_dir: Path):
    # empty
    empty = runner.invoke(app, ["cast", "list"])
    assert empty.exit_code == 0
    assert "no cast sheets" in empty.output
    # populate
    project = WritingProject.find()
    CastStore(project).save(CastSheet(slug="ruth-vann", name="Ruth Vann"))
    listed = runner.invoke(app, ["cast", "list"])
    assert listed.exit_code == 0 and "ruth-vann" in listed.output
    shown = runner.invoke(app, ["cast", "show", "ruth-vann"])
    assert shown.exit_code == 0 and "Ruth Vann" in shown.output
    # show missing slug fails cleanly
    missing = runner.invoke(app, ["cast", "show", "nobody"])
    assert missing.exit_code == 1


def test_cast_update_auto_and_dry_run(project_dir: Path, monkeypatch):
    calls: list[bool] = []

    def fake_update(project, chapter, model=None, provider=None, auto=False):
        calls.append(auto)
        return CastApplyResult(
            chapter=chapter,
            dry_run=not auto,
            applied=[{"slug": "ruth-vann", "kind": "knowledge", "id": "k001", "fact": "the bank called"}],
            conflicts=[CastConflict(slug="ruth-vann", kind="want_shift", detail="already set")],
        )

    monkeypatch.setattr(interiority, "run_cast_update", fake_update)

    auto = runner.invoke(app, ["cast", "update", "2", "--auto"])
    assert auto.exit_code == 0, auto.output
    assert "applied" in auto.output and "the bank called" in auto.output
    assert "conflict" in auto.output

    dry = runner.invoke(app, ["cast", "update", "2"])
    assert dry.exit_code == 0
    assert "would apply" in dry.output
    assert calls == [True, False]  # --auto then dry-run default


def test_cast_check_prints_findings(project_dir: Path, monkeypatch):
    from stoner.types import Finding, Severity

    def fake_check(project, chapter, model=None, provider=None):
        return CastReport(
            path="manuscript/ch-02.md",
            chapter=chapter,
            findings=[Finding(source="cast:boundedness", severity=Severity.major, category="anachronistic-knowledge", issue="acts early")],
            summary="1 violation",
        )

    monkeypatch.setattr(interiority, "run_cast_check", fake_check)
    result = runner.invoke(app, ["cast", "check", "2"])
    assert result.exit_code == 0, result.output
    assert "anachronistic-knowledge" in result.output


def test_cast_scene_prints_script(project_dir: Path, monkeypatch):
    def fake_scene(project, slugs, chapter, brief, model=None, provider=None, mode=None):
        return SceneResult(script="THE SCRIPT", transcript_path="/tmp/x.json", mode="multi", stopped_reason="all_passed")

    monkeypatch.setattr(interiority, "run_scene", fake_scene)
    result = runner.invoke(app, ["cast", "scene", "--who", "ruth-vann,dale-kestner", "--chapter", "2", "--brief", "they talk"])
    assert result.exit_code == 0, result.output
    assert "THE SCRIPT" in result.output
