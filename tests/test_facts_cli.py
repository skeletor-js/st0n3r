"""CLI tests for `stoner facts ...`. No network (typer CliRunner)."""

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
    root = tmp_path / "mybook"
    (root / "manuscript" / "ch-01.md").write_text(
        "---\ntitle: One\n---\n\nThe fee was $1,200 and the form was a CDFA-102.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    return root


def _enable_facts(root: Path) -> None:
    cfg = root / "stoner.yaml"
    text = cfg.read_text()
    # Flip the opt-in flag in the dumped config.
    text = text.replace("enabled: false", "enabled: true", 1) if "enabled: false" in text else text
    cfg.write_text(text)


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------


def test_facts_help_lists_commands(project_dir: Path):
    result = runner.invoke(app, ["facts", "--help"])
    assert result.exit_code == 0, result.output
    for cmd in ("research", "add", "list", "show", "sweep"):
        assert cmd in result.output


# ---------------------------------------------------------------------------
# add (offline)
# ---------------------------------------------------------------------------


def test_facts_add_offline(project_dir: Path):
    result = runner.invoke(
        app,
        [
            "facts", "add",
            "--name", "Reinspection fee",
            "--claim", "Category II carries a $1,200 reinspection fee",
            "--source-url", "https://humboldtgov.org/code",
            "--confidence", "high",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "added" in result.output
    assert (project_dir / "canon/facts/reinspection-fee.md").exists()


def test_facts_add_requires_source_url(project_dir: Path):
    result = runner.invoke(
        app,
        ["facts", "add", "--name", "X", "--claim", "some claim"],
    )
    assert result.exit_code != 0


def test_facts_add_conflict_surfaces(project_dir: Path):
    base = ["facts", "add", "--name", "Fee", "--source-url", "https://x.org"]
    r1 = runner.invoke(app, [*base, "--claim", "the fee is $1,200"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, [*base, "--claim", "the fee is $500"])
    assert r2.exit_code != 0
    assert "conflict" in r2.output.lower()


# ---------------------------------------------------------------------------
# research: opt-in gate + monkeypatched success
# ---------------------------------------------------------------------------


def test_facts_research_disabled_exits_nonzero(project_dir: Path):
    result = runner.invoke(app, ["facts", "research", "humboldt fees"])
    assert result.exit_code != 0
    assert "facts.enabled" in result.output


def test_facts_research_monkeypatched(project_dir: Path, monkeypatch):
    import stoner.facts.research as research_mod
    from stoner.facts.locker import FactRecord
    from stoner.facts.research import ResearchResult
    from stoner.types import Usage

    def fake_run_research(project, topic, **kwargs):
        return ResearchResult(
            topic=topic,
            path="native",
            dry_run=not kwargs.get("apply", False),
            candidates=[{"name": "F"}],
            applied=[
                FactRecord(
                    slug="fee",
                    name="Fee",
                    claim="the fee is $1,200",
                    source_url="https://humboldtgov.org",
                    confidence="high",
                )
            ],
            conflicts=[],
            usage=Usage(),
            notes=["a note"],
        )

    monkeypatch.setattr(research_mod, "run_research", fake_run_research)
    result = runner.invoke(app, ["facts", "research", "humboldt fees"])
    assert result.exit_code == 0, result.output
    assert "Fee" in result.output
    assert "a note" in result.output


# ---------------------------------------------------------------------------
# list / show
# ---------------------------------------------------------------------------


def test_facts_list_empty_then_populated(project_dir: Path):
    empty = runner.invoke(app, ["facts", "list"])
    assert empty.exit_code == 0, empty.output
    assert "no facts" in empty.output

    runner.invoke(
        app,
        ["facts", "add", "--name", "Fee", "--claim", "the fee is $1,200",
         "--source-url", "https://humboldtgov.org/code"],
    )
    listed = runner.invoke(app, ["facts", "list"])
    assert listed.exit_code == 0, listed.output
    assert "fee" in listed.output
    assert "humboldtgov.org" in listed.output


def test_facts_show_missing_slug_exits_nonzero(project_dir: Path):
    result = runner.invoke(app, ["facts", "show", "does-not-exist"])
    assert result.exit_code != 0
    assert "no fact" in result.output


def test_facts_show_existing(project_dir: Path):
    runner.invoke(
        app,
        ["facts", "add", "--name", "Fee", "--claim", "the fee is $1,200",
         "--source-url", "https://humboldtgov.org/code"],
    )
    result = runner.invoke(app, ["facts", "show", "fee"])
    assert result.exit_code == 0, result.output
    assert "the fee is $1,200" in result.output


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def test_facts_sweep_invokes_run_review_and_ledgers(project_dir: Path, monkeypatch):
    import json as _json

    import stoner.review.runner as runner_mod
    from stoner.types import ReviewReport

    captured = {}

    def fake_run_review(project, chapter, passes=None, model=None, provider=None):
        captured["passes"] = passes
        captured["chapter"] = chapter
        return ReviewReport(path=project.chapter_rel(chapter), passes=passes or [], findings=[])

    monkeypatch.setattr(runner_mod, "run_review", fake_run_review)
    result = runner.invoke(app, ["facts", "sweep", "1"])
    assert result.exit_code == 0, result.output
    assert captured["passes"] == ["verisimilitude"]
    assert captured["chapter"] == 1

    ledger = (project_dir / ".stoner/ledger.jsonl").read_text()
    actions = [_json.loads(ln)["action"] for ln in ledger.splitlines() if ln.strip()]
    assert "facts.sweep.run" in actions
