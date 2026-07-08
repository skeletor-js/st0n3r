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


# ---------------------------------------------------------------------------
# Promise & Motif Ledger (U6): promises / motifs sub-apps
# ---------------------------------------------------------------------------


def _write_chapter(root: Path, n: int, body: str) -> None:
    (root / "manuscript" / f"ch-{n:02d}.md").write_text(
        f"---\ntitle: ch{n}\nstatus: draft\n---\n\n{body}\n", encoding="utf-8"
    )


def _ledger_actions(root: Path) -> list[str]:
    import json as _json

    p = root / ".stoner" / "ledger.jsonl"
    if not p.exists():
        return []
    return [_json.loads(line)["action"] for line in p.read_text().splitlines() if line.strip()]


def test_promises_plant_list_and_ledger(project_dir: Path):
    res = runner.invoke(app, ["promises", "plant", "m1", "who is the ghost", "--kind", "mystery", "--opened-in", "ch-01"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["promises", "list"])
    assert res.exit_code == 0
    assert "m1" in res.output
    assert "mystery" in res.output
    assert "motif.promise.plant" in _ledger_actions(project_dir)


def test_promises_plant_invalid_kind_fails(project_dir: Path):
    res = runner.invoke(app, ["promises", "plant", "x", "bad", "--kind", "prophecy"])
    assert res.exit_code == 1


def test_promises_check_gates_on_open_then_passes(project_dir: Path):
    runner.invoke(app, ["promises", "plant", "m1", "who is the ghost", "--kind", "mystery", "--opened-in", "ch-01"])
    res = runner.invoke(app, ["promises", "check"])
    assert res.exit_code == 1  # open promise = unfired gun
    runner.invoke(app, ["promises", "payoff", "m1", "ch-09"])
    res = runner.invoke(app, ["promises", "check"])
    assert res.exit_code == 0
    assert "motif.promise.check" in _ledger_actions(project_dir)


def test_promises_check_strict_also_gates_plain_threads(project_dir: Path):
    # one open promise-kind row, one open plain (kind-less) thread
    runner.invoke(app, ["promises", "plant", "m1", "the promise", "--kind", "threat"])
    from stoner.canon.store import CanonStore
    from stoner.project import WritingProject

    CanonStore(WritingProject.find(project_dir)).add_thread("t1", "a plain thread", opened_in="ch-01")

    # pay off the promise so only the plain thread stays open
    runner.invoke(app, ["promises", "payoff", "m1", "ch-05"])
    res = runner.invoke(app, ["promises", "check"])
    assert res.exit_code == 0  # no open promises -> passes without --strict
    res = runner.invoke(app, ["promises", "check", "--strict"])
    assert res.exit_code == 1  # the open plain thread fails a strict loose-ends check
    assert "t1" in res.output


def test_promises_check_cites_only_promise_not_plain(project_dir: Path):
    runner.invoke(app, ["promises", "plant", "m1", "the mystery promise", "--kind", "mystery"])
    from stoner.canon.store import CanonStore
    from stoner.project import WritingProject

    CanonStore(WritingProject.find(project_dir)).add_thread("t1", "a plain thread", opened_in="ch-01")
    res = runner.invoke(app, ["promises", "check"])
    assert res.exit_code == 1
    assert "m1" in res.output
    assert "t1" not in res.output  # plain threads are only cited under --strict


def test_motifs_add_scan_json_and_report_file(project_dir: Path):
    import json as _json

    _write_chapter(project_dir, 1, "The river ran cold past the mill and the boy watched it go.")
    _write_chapter(project_dir, 2, "The market was loud and warm with the smell of fresh bread.")
    _write_chapter(project_dir, 3, "By the river she waited, counting the slow brown water at dusk.")
    res = runner.invoke(app, ["motifs", "add", "mo1", "the river", "--anchors", "river", "--meaning", "time"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["motifs", "scan", "--fmt", "json"])
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.output)
    assert payload["kind"] == "motifs"
    row = payload["rows"][0]
    assert row["per_chapter"] == {"1": 1, "3": 1}
    saved = list((project_dir / ".stoner" / "reviews").glob("motif-scan-*.json"))
    assert saved
    assert "motif.scan" in _ledger_actions(project_dir)


def test_motifs_scan_empty_is_friendly(project_dir: Path):
    res = runner.invoke(app, ["motifs", "scan", "--no-save"])
    assert res.exit_code == 0, res.output
    assert "no motifs" in res.output.lower()


def test_motifs_candidates_no_judge_offline(project_dir: Path):
    for n, body in {
        1: "The cold iron gate stood shut and the silver moon rose over the town.",
        2: "Again the cold iron gate barred her way as the silver moon climbed high.",
        3: "She passed the cold iron gate at dusk while the silver moon lit the road.",
    }.items():
        _write_chapter(project_dir, n, body)
    res = runner.invoke(app, ["motifs", "candidates", "--no-judge", "--min-chapters", "3"])
    assert res.exit_code == 0, res.output
    assert "cold iron gate" in res.output
    assert "motif.candidates" in _ledger_actions(project_dir)
