"""Tests for the `stoner readers` CLI group (U6). No network."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.canon.scaffold import scaffold_project
from stoner.cli.main import app
from stoner.project import WritingProject
from stoner.readers import bench as bench_mod
from stoner.readers import simulate as simulate_mod
from stoner.readers.bench import BenchResult
from stoner.readers.simulate import ReadersRunResult

runner = CliRunner()


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "book"
    proj = WritingProject.create(root, "Book")
    scaffold_project(proj, "Book")
    proj.write_chapter(1, {"title": "One"}, "The manuscript begins.\n")
    monkeypatch.chdir(root)
    return root


# ---------------------------------------------------------------------------
# personas + comps (no provider needed)
# ---------------------------------------------------------------------------


def test_personas_lists_shipped(project_dir: Path):
    result = runner.invoke(app, ["readers", "personas"])
    assert result.exit_code == 0, result.output
    assert "maya_riven" in result.output
    assert "personas" in result.output


def test_comps_add_and_list(project_dir: Path, tmp_path: Path):
    src = tmp_path / "src.txt"
    src.write_text("Chapter 1\n\nOne.\n\nChapter 2\n\nTwo.\n", encoding="utf-8")
    add = runner.invoke(app, ["readers", "comps", "add", str(src), "--title", "My Comp"])
    assert add.exit_code == 0, add.output
    assert "my-comp" in add.output
    listed = runner.invoke(app, ["readers", "comps", "list"])
    assert listed.exit_code == 0
    assert "my-comp" in listed.output


def test_comps_list_empty(project_dir: Path):
    result = runner.invoke(app, ["readers", "comps", "list"])
    assert result.exit_code == 0
    assert "no comps" in result.output


# ---------------------------------------------------------------------------
# heatmap on a project with no runs
# ---------------------------------------------------------------------------


def test_heatmap_without_runs_fails_cleanly(project_dir: Path):
    result = runner.invoke(app, ["readers", "heatmap"])
    assert result.exit_code == 1
    assert "No reader runs" in result.output


# ---------------------------------------------------------------------------
# run / bench via monkeypatched pipelines (avoids network)
# ---------------------------------------------------------------------------


def test_run_invokes_pipeline_and_prints_summary(project_dir: Path, monkeypatch):
    captured = {}

    def fake_run_readers(project, **kwargs):
        captured.update(kwargs)
        return ReadersRunResult(run_id="run-cli", chapters_read=1, calls=2, markers=5)

    def fake_run_heatmap(project, run_id):
        from stoner.readers.heatmap import HeatmapReport

        return HeatmapReport(run_id=run_id, roster_size=4), {}

    monkeypatch.setattr(simulate_mod, "run_readers", fake_run_readers)
    monkeypatch.setattr("stoner.readers.heatmap.run_heatmap", fake_run_heatmap)

    result = runner.invoke(app, ["readers", "run", "--chapters", "1-2", "--max-calls", "3"])
    assert result.exit_code == 0, result.output
    assert "run-cli" in result.output
    assert captured["chapters"] == [1, 2]
    assert captured["max_calls"] == 3


def test_bench_invokes_pipeline_and_prints_table(project_dir: Path, monkeypatch):
    def fake_run_bench(project, comp, **kwargs):
        return BenchResult(
            run_id="bench-cli",
            comp=comp,
            chapters_aligned=1,
            per_chapter=[{"chapter": 1, "manuscript": 3, "comp": 1, "draws": 0, "total": 4, "win_rate": 0.75}],
            manuscript_win_rate=0.75,
            aggregation="win-rate",
        )

    monkeypatch.setattr(bench_mod, "run_bench", fake_run_bench)
    result = runner.invoke(app, ["readers", "bench", "somecomp"])
    assert result.exit_code == 0, result.output
    assert "75%" in result.output
    assert "somecomp" in result.output


def test_readers_outside_project_exits_one(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["readers", "personas"])
    assert result.exit_code == 1
