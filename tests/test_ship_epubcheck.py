"""Optional epubcheck EPUB-spec validation (R9). epubcheck is a Java tool that
is not available here or in CI, so every test mocks the subprocess: discovery
is monkeypatched (`shutil.which`) and the runner is injected or patched. Covers
the tool-absent skip, the success and failure paths, an explicit config path,
and the CLI `--validate` flag wiring (skip never fails; a spec error exits 1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from stoner.canon.scaffold import scaffold_project
from stoner.cli.main import app
from stoner.ledger import Ledger
from stoner.project import WritingProject
from stoner.ship import epubcheck as ec

runner = CliRunner()


def _make_project(tmp_path: Path, chapters: int = 2) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "book")
    scaffold_project(p, "book")
    p.config.ship.title = "Test Book"
    p.config.ship.author = "A. Writer"
    for n in range(1, chapters + 1):
        p.write_chapter(
            n,
            {"title": f"Chapter {n}", "status": "revised"},
            f"First paragraph of chapter {n}.\n\nSecond one.",
        )
    return p


def _fake_runner(returncode: int, output: str):
    """A subprocess.run stand-in returning a fixed CompletedProcess."""

    def run(cmd, capture_output=True, text=True, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=output)

    return run


# ---------------------------------------------------------------------------
# discovery + validate_epub (unit)
# ---------------------------------------------------------------------------


def test_absent_tool_skips_without_error_or_ledger(tmp_path: Path, monkeypatch):
    from stoner.ship.epub import write_epub

    project = _make_project(tmp_path)
    path, _ = write_epub(project)
    monkeypatch.setattr(ec.shutil, "which", lambda _name: None)

    result = ec.validate_epub(project, path)
    assert result.available is False
    assert "epubcheck" in result.message
    # A skip is not an action: no ship.epubcheck ledger entry is written.
    assert all(e.action != "ship.epubcheck" for e in Ledger(project.root).tail(20))


def test_success_path_ledgers_and_reports_ok(tmp_path: Path, monkeypatch):
    from stoner.ship.epub import write_epub

    project = _make_project(tmp_path)
    path, _ = write_epub(project)
    monkeypatch.setattr(ec.shutil, "which", lambda _name: "/usr/bin/epubcheck")

    output = "Validating using EPUB version 3.3 rules.\nNo errors or warnings detected.\n"
    result = ec.validate_epub(project, path, runner=_fake_runner(0, output))
    assert result.available and result.ok
    assert result.errors == [] and result.warnings == []
    tail = Ledger(project.root).tail(1)[0]
    assert tail.action == "ship.epubcheck"
    assert tail.detail["ok"] is True and tail.detail["errors"] == 0


def test_failure_path_parses_errors_and_warnings(tmp_path: Path, monkeypatch):
    from stoner.ship.epub import write_epub

    project = _make_project(tmp_path)
    path, _ = write_epub(project)
    monkeypatch.setattr(ec.shutil, "which", lambda _name: "/usr/bin/epubcheck")

    output = (
        "Validating using EPUB version 3.3 rules.\n"
        "ERROR(RSC-005): book.epub/OEBPS/package.opf(2,10): bad metadata.\n"
        "WARNING(OPF-003): book.epub/OEBPS/style.css: item not in manifest.\n"
        "Check finished with errors\n"
    )
    result = ec.validate_epub(project, path, runner=_fake_runner(1, output))
    assert result.available and not result.ok
    assert len(result.errors) == 1 and "RSC-005" in result.errors[0]
    assert len(result.warnings) == 1 and "OPF-003" in result.warnings[0]
    tail = Ledger(project.root).tail(1)[0]
    assert tail.action == "ship.epubcheck"
    assert tail.detail["ok"] is False and tail.detail["errors"] == 1


def test_explicit_config_path_is_used(tmp_path: Path, monkeypatch):
    from stoner.ship.epub import write_epub

    project = _make_project(tmp_path)
    path, _ = write_epub(project)
    tool = tmp_path / "bin" / "epubcheck"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    project.config.ship.epubcheck_path = str(tool)
    # PATH lookup would miss it; the explicit is_file() fallback should win.
    monkeypatch.setattr(ec.shutil, "which", lambda _name: None)

    seen_cmd: list[str] = []

    def run(cmd, capture_output=True, text=True, **kwargs):
        seen_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = ec.validate_epub(project, path, runner=run)
    assert result.available and result.ok
    assert seen_cmd[0] == str(tool)


def test_explicit_missing_path_skips(tmp_path: Path, monkeypatch):
    project = _make_project(tmp_path)
    project.config.ship.epubcheck_path = str(tmp_path / "nope" / "epubcheck")
    monkeypatch.setattr(ec.shutil, "which", lambda _name: None)
    assert ec.find_epubcheck(project.config) is None


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_validate_absent_skips_and_exits_zero(tmp_path: Path, monkeypatch):
    project = _make_project(tmp_path)
    monkeypatch.setattr(ec.shutil, "which", lambda _name: None)
    monkeypatch.chdir(project.root)
    result = runner.invoke(app, ["ship", "epub", "--validate"])
    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    assert "epubcheck not found" in result.output


def test_cli_validate_failure_exits_one(tmp_path: Path, monkeypatch):
    project = _make_project(tmp_path)
    monkeypatch.setattr(ec.shutil, "which", lambda _name: "/usr/bin/epubcheck")
    monkeypatch.setattr(
        ec.subprocess,
        "run",
        _fake_runner(1, "ERROR(RSC-005): broken.epub: nope.\n"),
    )
    monkeypatch.chdir(project.root)
    result = runner.invoke(app, ["ship", "epub", "--validate"])
    assert result.exit_code == 1, result.output
    assert "epubcheck error" in result.output and "RSC-005" in result.output


def test_cli_no_validate_flag_skips_validation(tmp_path: Path, monkeypatch):
    project = _make_project(tmp_path)
    project.config.ship.epubcheck = True  # config default on (persisted to disk)...
    (project.root / "stoner.yaml").write_text(project.config.dump_yaml(), encoding="utf-8")
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        return None

    # ...but --no-validate on the command overrides it, so nothing runs.
    monkeypatch.setattr(ec, "validate_epub", boom)
    monkeypatch.chdir(project.root)
    result = runner.invoke(app, ["ship", "epub", "--no-validate"])
    assert result.exit_code == 0, result.output
    assert called["n"] == 0
