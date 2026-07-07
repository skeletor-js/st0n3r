"""Draft archaeology tests: the snapshot store/chokepoint (U1) and the
rewrite paths routed through it (U2). No network: providers are scripted
fakes, mirroring `tests/test_pipeline.py`."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from stoner.archaeology.snapshots import (
    DraftStore,
    body_hash,
    snapshot_write_chapter,
)
from stoner.canon.scaffold import scaffold_project
from stoner.engine.tools import write_chapter as write_chapter_tool
from stoner.ledger import Ledger
from stoner.pipelines.write import run_write
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.review.revise import revise_chapter
from stoner.types import CompletionRequest, CompletionResponse, Finding, Severity, Usage

BODY_ONE = (
    "He walked to the window and stood there a while. The yard was bare.\n\n"
    "A dog crossed the road and stopped, looked back, and went on.\n"
)
BODY_TWO = (
    "He thought of his father's hands, how they had held the plow.\n\n"
    "Nothing in the house moved. The kettle sat cold on the stove.\n"
)
BODY_THREE = (
    "In the morning there would be work: the fence along the north line.\n\n"
    "The clock in the hall kept its slow count.\n"
)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    return WritingProject.create(tmp_path / "book", "testbook")


def entries_of(project: WritingProject, number: int) -> list[dict]:
    manifest = json.loads(
        (project.root / ".stoner" / "drafts" / f"ch-{number:02d}" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    return manifest["entries"]


def snapshot_files(project: WritingProject, number: int) -> list[Path]:
    d = project.root / ".stoner" / "drafts" / f"ch-{number:02d}"
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.suffix == ".md")


# ---------------------------------------------------------------------------
# U1: snapshot store + chokepoint
# ---------------------------------------------------------------------------


def test_first_write_records_result_hash_only(project):
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_ONE, reason="draft")
    store = DraftStore(project)
    manifest = store.load_manifest(1)
    _, body = project.read_chapter(1)
    assert manifest.entries == []
    assert manifest.last_result_sha256 == body_hash(body)
    assert snapshot_files(project, 1) == []


def test_second_write_snapshots_prior_body(project):
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_ONE, reason="draft")
    _, first_body = project.read_chapter(1)
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_TWO, reason="draft")

    files = snapshot_files(project, 1)
    assert [f.name for f in files] == ["01-draft.md"]
    snap = files[0].read_text(encoding="utf-8")
    assert "He walked to the window" in snap
    assert snap.startswith("---\n")  # full chapter text: frontmatter + body

    (entry,) = entries_of(project, 1)
    _, current = project.read_chapter(1)
    assert entry["seq"] == 1
    assert entry["reason"] == "draft"
    assert entry["file"] == "01-draft.md"
    assert entry["sha256"] == body_hash(first_body)
    assert entry["result_sha256"] == body_hash(current)


def test_snapshot_chain_replays_to_current_body(project):
    for body in (BODY_ONE, BODY_TWO, BODY_THREE):
        snapshot_write_chapter(project, 1, {"title": "One"}, body, reason="draft")
    entries = entries_of(project, 1)
    assert len(entries) == 2
    # Each entry's result hash is the next entry's snapshotted hash; the
    # last result hash is the current on-disk body.
    assert entries[0]["result_sha256"] == entries[1]["sha256"]
    _, current = project.read_chapter(1)
    manifest = DraftStore(project).load_manifest(1)
    assert manifest.last_result_sha256 == body_hash(current)
    assert entries[1]["result_sha256"] == body_hash(current)
    # The snapshot files replay the prior states verbatim.
    files = snapshot_files(project, 1)
    assert "He walked to the window" in files[0].read_text(encoding="utf-8")
    assert "his father's hands" in files[1].read_text(encoding="utf-8")


def test_human_edit_detected_and_preserved(project):
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_ONE, reason="draft")
    # Hand-edit the chapter file between harness runs (not via the chokepoint).
    fm, _ = project.read_chapter(1)
    project.write_chapter(1, fm, "A hand-written paragraph the harness never saw.\n")

    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_TWO, reason="draft")

    entries = entries_of(project, 1)
    assert [e["reason"] for e in entries] == ["human-edit", "draft"]
    human, displaced = entries
    assert human["file"] == "01-human-edit.md"
    snap_dir = project.root / ".stoner" / "drafts" / "ch-01"
    assert "hand-written paragraph" in (snap_dir / human["file"]).read_text(encoding="utf-8")
    # The displacing event points at the same file (identical consecutive body).
    assert displaced["file"] == human["file"]
    assert displaced["sha256"] == human["sha256"]
    assert len(snapshot_files(project, 1)) == 1


def test_dedup_frontmatter_only_change_writes_no_file(project):
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_ONE, reason="draft")
    snapshot_write_chapter(project, 1, {"title": "Renamed"}, BODY_ONE, reason="draft")

    (entry,) = entries_of(project, 1)
    assert entry["file"] == ""
    assert entry["sha256"] == entry["result_sha256"]
    assert snapshot_files(project, 1) == []
    fm, _ = project.read_chapter(1)
    assert fm["title"] == "Renamed"


def test_corrupt_manifest_moved_to_bak_and_continues(project):
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_ONE, reason="draft")
    manifest_path = project.root / ".stoner" / "drafts" / "ch-01" / "manifest.json"
    manifest_path.write_text("{not json", encoding="utf-8")

    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_TWO, reason="draft")

    assert (manifest_path.parent / "manifest.json.bak").read_text(encoding="utf-8") == "{not json"
    # Snapshotting continued from a fresh manifest: the prior body was
    # still preserved before the overwrite.
    (entry,) = entries_of(project, 1)
    assert entry["reason"] == "draft"
    assert "He walked to the window" in (manifest_path.parent / entry["file"]).read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git on PATH")
def test_git_head_recorded_in_git_repo(project):
    root = project.root
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()

    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_ONE, reason="draft")
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_TWO, reason="draft")
    (entry,) = entries_of(project, 1)
    assert entry["git_head"] == head


def test_git_head_absent_outside_git_repo(project):
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_ONE, reason="draft")
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_TWO, reason="draft")
    (entry,) = entries_of(project, 1)
    assert "git_head" not in entry


def test_disabled_passes_straight_through(project):
    cfg_path = project.root / "stoner.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    data["archaeology"] = {"enabled": False}
    cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    project = WritingProject(project.root)  # reload config

    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_ONE, reason="draft")
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_TWO, reason="draft")

    _, body = project.read_chapter(1)
    assert "his father's hands" in body
    assert not (project.root / ".stoner" / "drafts" / "ch-01").exists()
    assert all(e.action != "drafts.snapshot" for e in Ledger(project.root).tail(50))


def test_every_snapshot_appends_drafts_snapshot_ledger_entry(project):
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_ONE, reason="draft")
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_TWO, reason="draft")
    fm, _ = project.read_chapter(1)
    project.write_chapter(1, fm, "Hand edit.\n")  # bypasses the chokepoint
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_THREE, reason="slop-revise")

    actions = [
        (e.detail.get("reason"), e.detail.get("seq"))
        for e in Ledger(project.root).tail(50)
        if e.action == "drafts.snapshot"
    ]
    assert actions == [("draft", 1), ("human-edit", 2), ("slop-revise", 3)]
    entries = Ledger(project.root).tail(50)
    snaps = [e for e in entries if e.action == "drafts.snapshot"]
    assert all(e.target == "manuscript/ch-01.md" for e in snaps)
    assert all(e.detail.get("chapter") == 1 for e in snaps)


# ---------------------------------------------------------------------------
# U2: existing rewrite paths route through the chokepoint
# ---------------------------------------------------------------------------


class ScriptedProvider(Provider):
    """Returns queued responses in order; repeats the last one when empty."""

    name = "scripted"
    supports_tools = True

    def __init__(self, responses: list[CompletionResponse]):
        self.responses = list(responses)
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def text_response(text: str) -> CompletionResponse:
    return CompletionResponse(text=text, stop_reason="end", usage=Usage())


CLEAN_PROSE = "\n\n".join(
    [
        "He walked to the window and stood there a while. The yard was bare. A dog crossed the road and stopped, looked back, and went on.",
        "He thought of his father's hands, how they had held the plow, and of the dry fields in August. Nothing in the house moved.",
        "He had lived here forty years and knew each sound the boards made, and none came now. The kettle sat cold on the stove.",
        "In the morning there would be work: the fence along the north line, the gate that sagged, a letter he owed his brother.",
        "She had left the garden in October, and the beds still held their shapes under the frost, patient as graves.",
        "Once, when he was a boy, his mother had read to him at this table by lamplight, her voice low against the wind outside.",
        "The clock in the hall kept its slow count. He did not wind it anymore, yet somehow it went on, losing a minute a day.",
        "A truck passed on the county road, its lights sweeping the ceiling, and the room returned to dark behind it.",
        "He ate standing up, bread and the end of the ham, and washed the plate and set it in the rack where hers used to go.",
        "Later he took down the ledger and entered the day's figures in his careful hand: feed, diesel, the vet's bill from spring.",
        "Sleep came the way it always came now, without announcement, somewhere between one worry and the next.",
        "Before dawn the birds began, first one and then the whole hedge, and he lay listening as the window went gray.",
    ]
)

SLOPPY_PROSE = (
    "She couldn't help but delve into the tapestry of emotions — a testament "
    "to the myriad feelings that washed over her. Little did she know, her "
    "eyes widened as a palpable sense of dread hung in the air. "
) * 12


@pytest.fixture()
def scaffolded(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "testbook")
    scaffold_project(p, "testbook")
    return p


def one_finding() -> list[Finding]:
    return [
        Finding(
            source="slop:word",
            severity=Severity.major,
            category="lexicon",
            issue="banned word",
        )
    ]


def revision_response(body: str) -> CompletionResponse:
    return text_response(f"SUMMARY: cleaned it up.\nBEGIN CHAPTER\n{body}\nEND CHAPTER\n")


def test_run_write_slop_loop_snapshots_pre_revise_body(scaffolded):
    provider = ScriptedProvider(
        [text_response(SLOPPY_PROSE), revision_response(CLEAN_PROSE)]
    )
    res = run_write(scaffolded, 1, provider=provider, skip_archive=True)
    assert res.revision_loops == 1
    assert res.gate_passed

    entries = entries_of(scaffolded, 1)
    assert [e["reason"] for e in entries] == ["slop-revise"]
    (snap,) = snapshot_files(scaffolded, 1)
    assert snap.name == "01-slop-revise.md"
    assert "delve into the tapestry" in snap.read_text(encoding="utf-8")
    _, body = scaffolded.read_chapter(1)
    assert "He walked to the window" in body


def test_revise_chapter_snapshots_prior_body(scaffolded):
    scaffolded.write_chapter(1, {"title": "One", "pov": "Mara"}, BODY_ONE)
    provider = ScriptedProvider([revision_response(BODY_TWO)])

    revise_chapter(scaffolded, 1, one_finding(), model="fake/m", provider=provider)

    (entry,) = entries_of(scaffolded, 1)
    assert entry["reason"] == "review-revise"
    assert entry["detail"] == {"applied": 1}
    (snap,) = snapshot_files(scaffolded, 1)
    assert snap.name == "01-review-revise.md"
    assert "He walked to the window" in snap.read_text(encoding="utf-8")
    fm, body = scaffolded.read_chapter(1)
    assert fm["status"] == "revised"
    assert "his father's hands" in body


def test_revise_chapter_refused_short_revision_snapshots_nothing(scaffolded):
    scaffolded.write_chapter(1, {"title": "One"}, CLEAN_PROSE)
    provider = ScriptedProvider([revision_response("Too short.")])

    with pytest.raises(ValueError, match="refusing to overwrite"):
        revise_chapter(scaffolded, 1, one_finding(), model="fake/m", provider=provider)

    _, body = scaffolded.read_chapter(1)
    assert body.strip() == CLEAN_PROSE.strip()
    assert not (scaffolded.root / ".stoner" / "drafts" / "ch-01" / "manifest.json").exists()
    assert snapshot_files(scaffolded, 1) == []


def test_revise_chapter_reason_kwarg_tags_snapshot(scaffolded):
    """book mode passes reason="book-revise" through revise_chapter."""
    scaffolded.write_chapter(1, {"title": "One"}, BODY_ONE)
    provider = ScriptedProvider([revision_response(BODY_TWO)])

    revise_chapter(
        scaffolded, 1, one_finding(), model="fake/m", provider=provider, reason="book-revise"
    )

    (entry,) = entries_of(scaffolded, 1)
    assert entry["reason"] == "book-revise"
    (snap,) = snapshot_files(scaffolded, 1)
    assert snap.name == "01-book-revise.md"


def test_agent_tool_redraft_snapshots_prior_body(project):
    out1 = write_chapter_tool(project, 5, title="Five", body=BODY_ONE)
    assert out1.startswith("Wrote manuscript/ch-05.md")
    out2 = write_chapter_tool(project, 5, title="Five", body=BODY_TWO)
    assert out2.startswith("Wrote manuscript/ch-05.md")

    (entry,) = entries_of(project, 5)
    assert entry["reason"] == "draft"
    (snap,) = snapshot_files(project, 5)
    assert snap.name == "01-draft.md"
    assert "He walked to the window" in snap.read_text(encoding="utf-8")
    _, body = project.read_chapter(5)
    assert "his father's hands" in body


# ---------------------------------------------------------------------------
# U3: provenance engine
# ---------------------------------------------------------------------------

PROV_ONE = (
    "The dog crossed the road and stopped. The kettle sat cold.\n\n"
    "A letter waited on the table.\n"
)
PROV_TWO = (
    "The dog crossed the road and stopped. The kettle sat cold.\n\n"
    "A letter waited on the table. The fence along the north line sagged.\n"
)
PROV_THREE = (
    "The dog crossed the road and stopped. The kettle sat cold on the stove.\n\n"
    "A letter waited on the table. The fence along the north line sagged.\n\n"
    "His father's hands had held the plow.\n"
)


def build_three_state_chain(project: WritingProject) -> None:
    snapshot_write_chapter(project, 1, {"title": "One"}, PROV_ONE, reason="draft")
    snapshot_write_chapter(
        project, 1, {"title": "One"}, PROV_TWO, reason="slop-revise", detail={"score": 40.0}
    )
    snapshot_write_chapter(
        project, 1, {"title": "One"}, PROV_THREE, reason="review-revise", detail={"applied": 2}
    )


def test_blame_attributes_untouched_and_added_sentences(project):
    from stoner.archaeology.provenance import attribute_sentences

    build_three_state_chain(project)
    rows = {r.sentence: r for r in attribute_sentences(project, 1)}

    untouched = rows["The dog crossed the road and stopped."]
    assert untouched.event == "initial"
    assert untouched.seq is None

    added_second = rows["The fence along the north line sagged."]
    assert added_second.event == "slop-revise"
    assert added_second.detail == {"score": 40.0}

    added_third = rows["His father's hands had held the plow."]
    assert added_third.event == "review-revise"
    assert added_third.detail == {"applied": 2}


def test_blame_classifies_reworded_sentence_as_revised(project):
    from stoner.archaeology.provenance import attribute_sentences

    build_three_state_chain(project)
    rows = {r.sentence: r for r in attribute_sentences(project, 1)}
    reworded = rows["The kettle sat cold on the stove."]
    assert reworded.event == "review-revise"
    assert reworded.revised
    assert reworded.originated_event == "initial"


def test_blame_is_deterministic(project):
    from stoner.archaeology.provenance import attribute_sentences

    build_three_state_chain(project)
    first = [r.model_dump_json() for r in attribute_sentences(project, 1)]
    second = [r.model_dump_json() for r in attribute_sentences(project, 1)]
    assert first == second


def test_blame_without_history_attributes_to_initial(project):
    from stoner.archaeology.provenance import attribute_sentences

    project.write_chapter(1, {"title": "One"}, PROV_ONE)  # never rewritten
    rows = attribute_sentences(project, 1)
    assert rows
    assert all(r.event == "initial" and r.seq is None for r in rows)


def test_blame_survives_pruned_middle_entry(project):
    from stoner.archaeology.provenance import attribute_sentences
    from stoner.archaeology.snapshots import prune_chapter_snapshots

    build_three_state_chain(project)
    snapshot_write_chapter(project, 1, {"title": "One"}, BODY_ONE, reason="draft")
    # Prune the middle of the chain (keep=1 protects seq 1 and the newest).
    res = prune_chapter_snapshots(project, 1, keep=1)
    assert res.pruned_seqs  # a middle entry really was pruned
    rows = attribute_sentences(project, 1)
    assert rows
    events = {r.event for r in rows}
    assert events  # both sides of the gap still resolve to events
    manifest = DraftStore(project).load_manifest(1)
    pruned = [e for e in manifest.entries if e.pruned]
    assert all(e.sha256 for e in pruned)  # hashes retained for the chain


def test_unified_diff_helper_shows_changed_lines():
    from stoner.archaeology.provenance import unified_diff

    diff = unified_diff("a\nb\n", "a\nc\n", "old", "new")
    assert "-b" in diff and "+c" in diff
    assert unified_diff("same\n", "same\n", "a", "b") == ""
