"""CLI + config tests for `stoner pacing report` (U5). No network: every
invocation uses --no-llm (typer CliRunner, tests/test_cli.py constraint)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.cli.main import app
from stoner.config import PacingConfig, StonerConfig
from stoner.pacing.instruments import scene_map
from stoner.pacing.segments import segment_chapter
from stoner.project import WritingProject

runner = CliRunner()

SCENE_BODY = (
    '"Count it again," Mara said.\n\n'
    '"I have counted it three times," Holt said. "The box is short and it '
    'was short before we ever left the harbor."\n'
)
SUMMARY_BODY = (
    "The voyage had taken nine weeks. By the time they had sighted land the "
    "crew had eaten through the stores, and over the next days they had "
    "traded the spare canvas for fruit and salted fish.\n"
)


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    result = runner.invoke(app, ["init", "mybook", "--path", str(tmp_path / "mybook")])
    assert result.exit_code == 0, result.output
    root = tmp_path / "mybook"
    (root / "manuscript" / "ch-01.md").write_text(
        f"---\ntitle: One\npov: Mara\n---\n\n{SCENE_BODY}", encoding="utf-8"
    )
    (root / "manuscript" / "ch-02.md").write_text(
        f"---\ntitle: Two\npov: Mara\n---\n\n{SUMMARY_BODY}", encoding="utf-8"
    )
    monkeypatch.chdir(root)
    return root


def test_pacing_report_no_llm_writes_report(project_dir: Path):
    result = runner.invoke(app, ["pacing", "report", "--no-llm"])
    assert result.exit_code == 0, result.output
    assert "Pacing report" in result.output
    saved = list((project_dir / ".stoner" / "reviews").glob("pacing-*.json"))
    assert len(saved) == 1
    payload = json.loads(saved[0].read_text(encoding="utf-8"))
    assert payload["kind"] == "pacing"
    assert len(payload["series"]) == 2


def test_pacing_report_json_format_parses(project_dir: Path):
    result = runner.invoke(app, ["pacing", "report", "--no-llm", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "pacing"
    assert payload["llm"] is False


def test_pacing_report_no_save_writes_nothing(project_dir: Path):
    result = runner.invoke(app, ["pacing", "report", "--no-llm", "--no-save"])
    assert result.exit_code == 0, result.output
    assert list((project_dir / ".stoner" / "reviews").glob("pacing-*")) == []


def test_pacing_report_unknown_format_fails(project_dir: Path):
    result = runner.invoke(app, ["pacing", "report", "--no-llm", "--format", "html"])
    assert result.exit_code == 1


def test_pacing_report_without_chapters_fails_cleanly(tmp_path: Path, monkeypatch):
    result = runner.invoke(app, ["init", "empty", "--path", str(tmp_path / "empty")])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path / "empty")
    result = runner.invoke(app, ["pacing", "report", "--no-llm"])
    assert result.exit_code == 1


def test_pacing_report_outside_project_exits_one(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["pacing", "report", "--no-llm"])
    assert result.exit_code == 1


# -- config --------------------------------------------------------------------


def test_yaml_pacing_block_overrides_scene_threshold(project_dir: Path):
    (project_dir / "stoner.yaml").write_text(
        "project_name: mybook\npacing:\n  in_scene_min_ratio: 0.5\n", encoding="utf-8"
    )
    project = WritingProject(project_dir)
    assert project.config.pacing.in_scene_min_ratio == 0.5

    # a chapter that fails the default 0.70 target passes at 0.5
    from stoner.pacing.data import ChapterData
    from stoner.project import count_words

    body = SCENE_BODY + "\n" + SCENE_BODY + "\n" + SUMMARY_BODY
    ch = ChapterData(number=1, body=body, words=count_words(body), segments=segment_chapter(body))
    assert 0.5 <= ch.segments.in_scene_fraction < 0.70
    assert scene_map([ch], PacingConfig()).findings  # default flags it
    assert scene_map([ch], project.config.pacing).findings == []  # 0.5 does not


def test_config_default_round_trip_includes_pacing_block():
    dumped = StonerConfig().dump_yaml()
    assert "pacing:" in dumped
    assert "in_scene_min_ratio: 0.7" in dumped
    assert "llm_instruments: true" in dumped


def test_yaml_without_pacing_block_still_loads(tmp_path: Path):
    (tmp_path / "stoner.yaml").write_text("project_name: old-project\n", encoding="utf-8")
    cfg = StonerConfig.load(tmp_path)
    assert cfg.pacing.in_scene_min_ratio == 0.70
    assert cfg.pacing.flatline_min_run == 3
    assert cfg.pacing.llm_instruments is True
