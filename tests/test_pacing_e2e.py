"""End-to-end calibration (U7): the pacing layer must separate a FLAT book
from a SHAPED book, directionally (the tests/test_slop.py paired-fixture
stance -- separation, not exact values). Judge calls are scripted; the
deterministic half is asserted stable across consecutive runs."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.canon.memory import Memory
from stoner.canon.scaffold import scaffold_project
from stoner.cli.main import app
from stoner.ledger import Ledger
from stoner.pacing import run_pacing
from stoner.pacing.report import to_payload
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.types import CompletionRequest, CompletionResponse, Severity, Usage

runner = CliRunner()


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, responses: list | Callable[[int], CompletionResponse]):
        self.responses = responses
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        if callable(self.responses):
            item = self.responses(idx)
        else:
            item = self.responses[min(idx, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _resp(tension: str, changes: list[str] | None = None) -> CompletionResponse:
    payload = {"tension": tension, "tension_why": "x", "changes_hands": changes or [], "beats": []}
    return CompletionResponse(
        text=json.dumps(payload), usage=Usage(input_tokens=10, output_tokens=5)
    )


# ---------------------------------------------------------------------------
# FLAT_BOOK: summary-heavy, one ending shape, single POV, uniform lengths,
# judge scripted to holds/sags with empty changes-hands.
# ---------------------------------------------------------------------------

FLAT_CHAPTER = (
    "Mara had spent the season keeping the ledgers. By the time the frost "
    "came she had filed every receipt, and over the next weeks she had grown "
    "used to the silence of the counting room.\n\n"
    "The estate had run itself, more or less. The tenants had paid what they "
    "had always paid, and the letters from town had said what they had "
    "always said. She had answered a few and had burned the rest.\n\n"
    "Winter had settled in by then. The roads had closed, the deliveries had "
    "stopped, and the household had shrunk to the four of them and the dog.\n\n"
    "The house stayed quiet.\n"
)

FLAT_JUDGE = [_resp("opens"), _resp("holds"), _resp("sags"), _resp("holds"), _resp("sags")]

# ---------------------------------------------------------------------------
# SHAPED_BOOK: dialogue-forward scenes, varied endings, alternating POV,
# varied lengths, judge scripted to rises with populated ledgers.
# ---------------------------------------------------------------------------

_SCENE = (
    '"You counted it yourself," Mara said, sliding the strongbox across the '
    'table so hard it cracked the veneer.\n\n'
    '"I counted what was left," Holt said. "There is a difference, and you '
    'know there is a difference, and shouting at me will not close it."\n\n'
    'She pulled the pistol from the drawer and set it between them, and '
    'neither of them looked at it while the clock worked through the minute.\n'
)

_SHAPED_ENDINGS = [
    '"Then we ride tonight," Holt said, and the room emptied around them.\n',  # dialogue
    "And if the letters were forged, who had held the pen?\n",  # question
    "She walked the wall until dawn came up over the harbor and the ships "
    "swung slowly on their anchors toward the open sea, sails loose, decks "
    "crowded, every one of them flying the wrong flag for this coast.\n",  # cliff
    "The bridge was gone.\n",  # punch
    '"Open the gates," Mara said.\n',  # dialogue (no 3-run: D Q C P D)
]

SHAPED_JUDGE = [
    _resp("opens", ["Mara takes command of the garrison"]),
    _resp("rises", ["Holt learns the letters were forged"]),
    _resp("holds", ["the strongbox passes to the bank"]),
    _resp("rises", ["the bridge is destroyed"]),
    _resp("rises", ["the fleet declares for the pretender"]),
]


def _build_flat(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "flat", "Flat Book")
    scaffold_project(proj, "Flat Book")
    memory = Memory(proj)
    for n in range(1, 6):
        proj.write_chapter(n, {"title": f"Ch{n}", "pov": "Mara"}, FLAT_CHAPTER)
        memory.set_chapter_summary(n, f"Chapter {n}: the season passes quietly.", pov="Mara")
    return proj


def _build_shaped(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "shaped", "Shaped Book")
    scaffold_project(proj, "Shaped Book")
    memory = Memory(proj)
    povs = ["Mara", "Holt", "Mara", "Holt", "Mara"]
    scales = [2, 3, 5, 2, 4]  # varied lengths
    for n in range(1, 6):
        body = "\n".join([_SCENE] * scales[n - 1]) + "\n" + _SHAPED_ENDINGS[n - 1]
        proj.write_chapter(n, {"title": f"Ch{n}", "pov": povs[n - 1]}, body)
        memory.set_chapter_summary(n, f"Chapter {n}: something irreversible happens.", pov=povs[n - 1])
    return proj


@pytest.fixture()
def flat(tmp_path: Path) -> WritingProject:
    return _build_flat(tmp_path)


@pytest.fixture()
def shaped(tmp_path: Path) -> WritingProject:
    return _build_shaped(tmp_path)


# ---------------------------------------------------------------------------
# separation
# ---------------------------------------------------------------------------


def test_flat_vs_shaped_separation(flat: WritingProject, shaped: WritingProject):
    flat_report = run_pacing(flat, llm=True, provider=FakeProvider(list(FLAT_JUDGE)))
    shaped_report = run_pacing(shaped, llm=True, provider=FakeProvider(list(SHAPED_JUDGE)))

    # FLAT: flatline run + ending echo + scene starvation
    assert flat_report.flatlines, "flat book must produce a flatline run"
    assert any(f.source == "pacing:flatline" for f in flat_report.findings)
    assert any(f.source == "pacing:ending_echo" for f in flat_report.findings)
    flat_scene = [f for f in flat_report.findings if f.source == "pacing:scene_map"]
    shaped_scene = [f for f in shaped_report.findings if f.source == "pacing:scene_map"]
    assert len(flat_scene) > len(shaped_scene)

    # SHAPED: zero major/critical findings
    shaped_majors = [
        f for f in shaped_report.findings
        if f.severity in (Severity.major, Severity.critical)
    ]
    assert shaped_majors == []
    assert shaped_report.flatlines == []

    # tension series match the scripts
    assert [r["tension"] for r in flat_report.series] == ["opens", "holds", "sags", "holds", "sags"]
    assert all(r["changes_hands"] == [] for r in flat_report.series[1:])
    assert all(r["changes_hands"] for r in shaped_report.series)

    # structural lanes separate: uniform vs varied
    assert any("dead_uniform" in f.category for f in flat_report.findings)
    assert not any("dead_uniform" in f.category for f in shaped_report.findings)
    flat_shapes = {r["ending_shape"] for r in flat_report.series}
    shaped_shapes = {r["ending_shape"] for r in shaped_report.series}
    assert len(flat_shapes) == 1
    assert len(shaped_shapes) >= 3


def test_flat_report_json_carries_flatline_diagnostic(flat: WritingProject):
    report = run_pacing(flat, llm=True, provider=FakeProvider(list(FLAT_JUDGE)))
    raw = Path(report.json_path).read_text(encoding="utf-8")
    assert "flatline; nothing changes hands." in raw
    payload = json.loads(raw)
    assert payload["kind"] == "pacing"
    assert payload["flatlines"] == [[2, 5]]
    issues = [f["issue"] for f in payload["findings"]]
    assert "Chapters 2–5 flatline; nothing changes hands." in issues


# ---------------------------------------------------------------------------
# ledger + cache across runs
# ---------------------------------------------------------------------------


def test_ledger_records_report_and_judge_with_cache_flags(flat: WritingProject):
    run_pacing(flat, llm=True, provider=FakeProvider(list(FLAT_JUDGE)))
    rerun_provider = FakeProvider(list(FLAT_JUDGE))
    run_pacing(flat, llm=True, provider=rerun_provider)

    assert rerun_provider.requests == []  # cache did all the work

    entries = Ledger(flat.root).tail(50)
    reports = [e for e in entries if e.action == "pacing.report"]
    judges = [e for e in entries if e.action == "pacing.judge"]
    assert len(reports) == 2
    assert len(judges) == 10  # 5 fresh + 5 cached
    assert sum(1 for e in judges if e.detail.get("cached")) == 5
    assert sum(1 for e in judges if not e.detail.get("cached")) == 5
    # only pacing.* actions were added by this feature
    assert all(e.action.startswith("pacing.") or e.action == "project.init" for e in entries)


# ---------------------------------------------------------------------------
# CLI path + determinism
# ---------------------------------------------------------------------------


def test_cli_no_llm_path_on_flat_book(flat: WritingProject, monkeypatch):
    monkeypatch.chdir(flat.root)
    result = runner.invoke(app, ["pacing", "report", "--no-llm"])
    assert result.exit_code == 0, result.output
    saved = list((flat.root / ".stoner" / "reviews").glob("pacing-*.json"))
    assert len(saved) == 1
    payload = json.loads(saved[0].read_text(encoding="utf-8"))
    assert payload["llm"] is False
    assert len(payload["series"]) == 5


def test_deterministic_half_is_stable_across_runs(flat: WritingProject):
    a = run_pacing(flat, llm=False, save=False)
    b = run_pacing(flat, llm=False, save=False)

    def normalize(report):
        payload = to_payload(report)
        payload.pop("created_at")
        for f in payload["findings"]:
            f.pop("id")  # random uuids; everything else must match
        return payload

    assert normalize(a) == normalize(b)
