"""Tests for draft-archaeology refactors: renumbering (U5), merge/split (U6),
model-assisted move-reveal/flip-pov (U7), and post-refactor verification
(U8). No network: providers are scripted fakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.archaeology.refactor import (
    RefactorError,
    flip_pov,
    merge_chapters,
    move_reveal,
    split_chapter,
)
from stoner.archaeology.renumber import (
    apply_renumber,
    check_integrity,
    plan_renumber,
)
from stoner.archaeology.snapshots import snapshot_write_chapter
from stoner.canon.memory import Memory
from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.ledger import Ledger
from stoner.pipelines.book import BookState, ChapterDone, save_state
from stoner.project import WritingProject
from stoner.providers.base import Provider, ProviderError
from stoner.types import CompletionRequest, CompletionResponse, Severity, Usage

runner = CliRunner()


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


class RaisingProvider(Provider):
    name = "raising"
    supports_tools = True

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        raise ProviderError("no key set for scripted failure")


def text_response(text: str) -> CompletionResponse:
    return CompletionResponse(text=text, stop_reason="end", usage=Usage(output_tokens=7))


def sentinel_response(body: str) -> CompletionResponse:
    return text_response(f"SUMMARY: restructured.\nBEGIN CHAPTER\n{body}\nEND CHAPTER\n")


def chapter_body(n: int) -> str:
    return (
        f"Chapter {n} opens on the north field where the fence sagged in the frost.\n\n"
        f"He counted the posts twice and wrote the number in the ledger, chapter {n}'s "
        "habit of care.\n\n"
        f"By dusk the work of chapter {n} was done and the kettle went on the stove.\n\n"
        f"A letter from his brother sat unopened on the table of chapter {n}.\n\n"
        f"Sleep came late in chapter {n}, somewhere between one worry and the next.\n"
    )


def seed_project(root: Path, numbers: list[int]) -> WritingProject:
    """A project with chapters, beat sheets, memory, book-state, and a thread
    row per chapter — the full renumber blast radius."""
    project = WritingProject.create(root, "testbook")
    scaffold_project(project, "testbook")
    store = CanonStore(project)
    state = BookState()
    for n in numbers:
        snapshot_write_chapter(
            project, n, {"title": f"Chapter {n}", "pov": "Mara"}, chapter_body(n), reason="draft"
        )
        beats = project.root / f"outline/beats/ch-{n:02d}.md"
        beats.parent.mkdir(parents=True, exist_ok=True)
        beats.write_text(f"---\nchapter: {n}\n---\n\n- beat one of {n}\n", encoding="utf-8")
        Memory(project).set_chapter_summary(n, f"Summary of chapter {n}.", pov="Mara", words=100)
        state.chapters_planned.append(n)
        state.chapters_done[n] = ChapterDone(words=100)
    save_state(project, state)
    store.add_thread("t1", "the fence", opened_in="1", status="open")
    return project


def entries_of(project: WritingProject, number: int) -> list[dict]:
    p = project.root / ".stoner" / "drafts" / f"ch-{number:02d}" / "manifest.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))["entries"]


# ---------------------------------------------------------------------------
# U5: renumbering engine + integrity checks
# ---------------------------------------------------------------------------


def test_shift_down_renumbers_full_blast_radius(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3, 5, 6])
    store = CanonStore(project)
    store.update_thread("t1", opened_in="5", resolved_in="6")
    store.add_timeline_row("day 3", "the fence falls", chapters="ch-05")

    mapping = {5: 4, 6: 5}
    plan = plan_renumber(project, mapping)
    apply_renumber(project, plan)

    assert sorted(c.number for c in project.chapters()) == [1, 2, 3, 4, 5]
    _fm, body = project.read_chapter(4)
    assert "Chapter 5 opens" in body
    assert (project.root / "outline/beats/ch-04.md").exists()
    assert not (project.root / "outline/beats/ch-06.md").exists()
    assert (project.root / ".stoner/drafts/ch-04").exists()
    assert not (project.root / ".stoner/drafts/ch-06").exists()

    memory = project.read_memory()["chapters"]
    assert "Summary of chapter 5." == memory["4"]["summary"]
    assert "6" not in memory and "4" in memory and "5" in memory

    from stoner.pipelines.book import load_state

    state = load_state(project)
    assert sorted(state.chapters_planned) == [1, 2, 3, 4, 5]
    assert 6 not in state.chapters_done and 4 in state.chapters_done

    threads = CanonStore(project).threads()
    assert threads[0].opened_in == "4"
    assert threads[0].resolved_in == "5"
    rows = CanonStore(project).timeline_rows()
    assert rows[0].chapters == "ch-04"


def test_unparseable_thread_cell_left_untouched_and_flagged(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2])
    CanonStore(project).update_thread("t1", opened_in="mid-book")
    plan = plan_renumber(project, {2: 3})
    assert any("mid-book" in f for f in plan.flags)
    apply_renumber(project, plan)
    assert CanonStore(project).threads()[0].opened_in == "mid-book"


def test_overlapping_mapping_applies_without_clobbering(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    plan = plan_renumber(project, {2: 3, 3: 4})
    apply_renumber(project, plan)
    assert sorted(c.number for c in project.chapters()) == [1, 3, 4]
    _fm, body3 = project.read_chapter(3)
    _fm, body4 = project.read_chapter(4)
    assert "Chapter 2 opens" in body3
    assert "Chapter 3 opens" in body4


def test_snapshot_first_before_rename(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2])
    plan = plan_renumber(project, {2: 3})
    apply_renumber(project, plan, reason="refactor-split")
    # The renamed chapter's manifest (moved with the dir) holds its body
    # snapshotted under the original number before the move.
    entries = entries_of(project, 3)
    snaps = [e for e in entries if e["reason"] == "refactor-split"]
    assert snaps
    snap_dir = project.root / ".stoner" / "drafts" / "ch-03"
    assert "Chapter 2 opens" in (snap_dir / snaps[-1]["file"]).read_text(encoding="utf-8")


def test_integrity_checker_healthy_orphan_and_gap(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    findings = check_integrity(project)
    assert not any(f.severity in (Severity.major, Severity.critical) for f in findings)

    # Delete a chapter, leaving its beats file orphaned and a numbering gap.
    (project.root / "manuscript" / "ch-02.md").unlink()
    findings = check_integrity(project)
    cats = {f.category for f in findings if f.severity == Severity.major}
    assert "orphan-beats" in cats
    assert "chapter-gap" in cats


# ---------------------------------------------------------------------------
# U6: deterministic refactors merge / split
# ---------------------------------------------------------------------------


def test_merge_4_and_5_in_seven_chapter_project(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3, 4, 5, 6, 7])
    body4 = project.read_chapter(4)[1]
    body5 = project.read_chapter(5)[1]

    res = merge_chapters(project, 4, 5, verify=False)

    assert sorted(c.number for c in project.chapters()) == [1, 2, 3, 4, 5, 6]
    _fm, merged = project.read_chapter(4)
    assert body4.strip() in merged
    assert body5.strip() in merged
    assert merged.index("Chapter 4 opens") < merged.index("Chapter 5 opens")
    _fm, new5 = project.read_chapter(5)
    assert "Chapter 6 opens" in new5
    _fm, new6 = project.read_chapter(6)
    assert "Chapter 7 opens" in new6

    beats4 = (project.root / "outline/beats/ch-04.md").read_text(encoding="utf-8")
    assert "beat one of 4" in beats4 and "beat one of 5" in beats4
    assert "beat one of 6" in (project.root / "outline/beats/ch-05.md").read_text(encoding="utf-8")
    memory = project.read_memory()["chapters"]
    assert "Summary of chapter 4." in memory["4"]["summary"]
    assert "Summary of chapter 5." in memory["4"]["summary"]
    assert memory["5"]["summary"] == "Summary of chapter 6."
    assert "7" not in memory

    assert not res.integrity_failed
    assert res.mapping == {6: 5, 7: 6}

    # Both original bodies are restorable byte-for-byte from ch-04 snapshots.
    from stoner.archaeology.restore import restore_chapter

    entries = entries_of(project, 4)
    seq_b = next(e["seq"] for e in entries if e["detail"].get("merged_from") == 5 and e["file"])
    restore_chapter(project, 4, seq_b)
    assert project.read_chapter(4)[1] == body5
    seq_a = next(
        e["seq"]
        for e in entries
        if e["reason"] == "refactor-merge" and e["seq"] != seq_b and e["file"]
    )
    restore_chapter(project, 4, seq_a)
    assert project.read_chapter(4)[1] == body4

    ledger_entries = [e for e in Ledger(project.root).tail(50) if e.action == "drafts.refactor.merge"]
    assert len(ledger_entries) == 1
    assert ledger_entries[0].detail["mapping"] == {"6": 5, "7": 6}


def test_split_chapter_3_at_paragraph_4(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3, 4, 5])
    blocks = [p for p in project.read_chapter(3)[1].split("\n\n") if p.strip()]

    res = split_chapter(project, 3, at=4, verify=False)

    assert sorted(c.number for c in project.chapters()) == [1, 2, 3, 4, 5, 6]
    _fm, first = project.read_chapter(3)
    _fm, second = project.read_chapter(4)
    assert first.strip() == "\n\n".join(blocks[:3]).strip()
    assert second.strip() == "\n\n".join(blocks[3:]).strip()
    _fm, new5 = project.read_chapter(5)
    assert "Chapter 4 opens" in new5
    _fm, new6 = project.read_chapter(6)
    assert "Chapter 5 opens" in new6

    assert res.mapping == {4: 5, 5: 6}
    assert not res.integrity_failed
    # The new second half is flagged as beat-less (advisory).
    assert any(
        f.category == "missing-beats" and "ch-04" in f.issue for f in res.findings
    )
    ledger_entries = [e for e in Ledger(project.root).tail(50) if e.action == "drafts.refactor.split"]
    assert len(ledger_entries) == 1
    assert ledger_entries[0].detail["mapping"] == {"4": 5, "5": 6}


def test_merge_preserves_hand_edit_as_human_edit_snapshot(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    fm, _ = project.read_chapter(2)
    project.write_chapter(2, fm, "A hand-edited chapter two the harness never saw.\n\nStill here.\n")

    merge_chapters(project, 2, 3, verify=False)

    entries = entries_of(project, 2)
    human = [e for e in entries if e["reason"] == "human-edit"]
    assert human
    snap_dir = project.root / ".stoner" / "drafts" / "ch-02"
    assert "hand-edited chapter two" in (snap_dir / human[0]["file"]).read_text(encoding="utf-8")
    # The hand-edit is also the text that got merged.
    _fm, merged = project.read_chapter(2)
    assert "hand-edited chapter two" in merged


def test_split_out_of_range_fails_before_touching_files(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    before = {
        n: (project.root / f"manuscript/ch-{n:02d}.md").read_text(encoding="utf-8")
        for n in (1, 2, 3)
    }
    before_entries = {n: entries_of(project, n) for n in (1, 2, 3)}

    with pytest.raises(RefactorError, match="--at must be"):
        split_chapter(project, 2, at=99, verify=False)
    with pytest.raises(RefactorError, match="not found"):
        split_chapter(project, 2, at_text="no such marker", verify=False)

    for n in (1, 2, 3):
        assert (project.root / f"manuscript/ch-{n:02d}.md").read_text(encoding="utf-8") == before[n]
        assert entries_of(project, n) == before_entries[n]


def test_merge_requires_adjacent_and_existing_chapters(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    with pytest.raises(RefactorError, match="adjacent"):
        merge_chapters(project, 1, 3, verify=False)
    with pytest.raises(RefactorError, match="not found"):
        merge_chapters(project, 3, 4, verify=False)


def test_merge_then_restore_round_trip_via_cli(tmp_path: Path, monkeypatch):
    from stoner.cli.main import app

    project = seed_project(tmp_path / "book", [1, 2, 3])
    monkeypatch.chdir(project.root)
    body2 = project.read_chapter(2)[1]

    result = runner.invoke(app, ["drafts", "refactor", "merge", "2", "3", "--no-verify"])
    assert result.exit_code == 0, result.output
    assert "merged" in result.output

    entries = entries_of(project, 2)
    seq_a = next(
        e["seq"]
        for e in entries
        if e["reason"] == "refactor-merge" and not e["detail"].get("merged_from") == 3
    )
    result = runner.invoke(app, ["drafts", "restore", "2", str(seq_a)])
    assert result.exit_code == 0, result.output
    assert project.read_chapter(2)[1] == body2


# ---------------------------------------------------------------------------
# U7: model-assisted refactors move-reveal / flip-pov
# ---------------------------------------------------------------------------

REVEAL = "A letter from his brother sat unopened on the table of chapter 2."


def _src_without_reveal(body: str) -> str:
    return body.replace(REVEAL, "The table stood bare where the letter had been.")


def _dst_with_reveal(body: str) -> str:
    return REVEAL + "\n\n" + body


def test_move_reveal_moves_quote_and_snapshots_both(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    body_src = project.read_chapter(2)[1]
    body_dst = project.read_chapter(3)[1]
    provider = ScriptedProvider(
        [
            sentinel_response(_src_without_reveal(body_src)),
            sentinel_response(_dst_with_reveal(body_dst)),
        ]
    )

    res = move_reveal(project, 2, 3, REVEAL, provider=provider, verify=False)

    _fm, new_src = project.read_chapter(2)
    _fm, new_dst = project.read_chapter(3)
    assert REVEAL not in new_src
    assert REVEAL in new_dst
    assert res.usage.output_tokens == 14  # both completions counted

    for n, old_body in ((2, body_src), (3, body_dst)):
        entries = entries_of(project, n)
        moves = [e for e in entries if e["reason"] == "refactor-move"]
        assert moves, f"ch-{n} missing refactor-move snapshot"
        snap_dir = project.root / ".stoner" / "drafts" / f"ch-{n:02d}"
        assert old_body.strip() in (snap_dir / moves[-1]["file"]).read_text(encoding="utf-8")

    assert any(e.action == "drafts.refactor.move" for e in Ledger(project.root).tail(20))


def test_move_reveal_quote_not_found_fails_before_model_call(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    provider = ScriptedProvider([sentinel_response("never used")])
    with pytest.raises(RefactorError, match="quote not found"):
        move_reveal(project, 2, 3, "no such reveal anywhere", provider=provider, verify=False)
    assert provider.requests == []


def test_move_reveal_truncated_response_refuses_and_touches_nothing(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    before2 = (project.root / "manuscript/ch-02.md").read_text(encoding="utf-8")
    before3 = (project.root / "manuscript/ch-03.md").read_text(encoding="utf-8")
    provider = ScriptedProvider([sentinel_response("Too short.")])

    with pytest.raises(RefactorError, match="refusing to overwrite"):
        move_reveal(project, 2, 3, REVEAL, provider=provider, verify=False)

    assert (project.root / "manuscript/ch-02.md").read_text(encoding="utf-8") == before2
    assert (project.root / "manuscript/ch-03.md").read_text(encoding="utf-8") == before3
    assert not [e for e in entries_of(project, 2) if e["reason"] == "refactor-move"]
    assert not [e for e in entries_of(project, 3) if e["reason"] == "refactor-move"]


def test_move_reveal_second_completion_truncated_leaves_source_untouched(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    before2 = (project.root / "manuscript/ch-02.md").read_text(encoding="utf-8")
    body_src = project.read_chapter(2)[1]
    provider = ScriptedProvider(
        [sentinel_response(_src_without_reveal(body_src)), sentinel_response("stub")]
    )
    with pytest.raises(RefactorError, match="refusing to overwrite"):
        move_reveal(project, 2, 3, REVEAL, provider=provider, verify=False)
    assert (project.root / "manuscript/ch-02.md").read_text(encoding="utf-8") == before2


def test_flip_pov_updates_frontmatter_and_snapshots(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2])
    body = project.read_chapter(1)[1]
    rewritten = body.replace("He counted the posts", "Tomas counted the posts")
    # Response WITHOUT sentinels: the tolerant fallback still extracts it.
    provider = ScriptedProvider([text_response(rewritten)])

    res = flip_pov(project, 1, "Tomas", provider=provider, verify=False)

    fm, new_body = project.read_chapter(1)
    assert fm["pov"] == "Tomas"
    assert "Tomas counted the posts" in new_body
    entries = entries_of(project, 1)
    assert entries[-1]["reason"] == "refactor-pov"
    assert any(n.startswith("no canon entry") for n in res.notes)
    assert any(e.action == "drafts.refactor.pov" for e in Ledger(project.root).tail(20))


def test_model_refactors_work_with_text_only_provider(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    body_src = project.read_chapter(2)[1]
    body_dst = project.read_chapter(3)[1]

    class TextOnly(ScriptedProvider):
        supports_tools = False

    provider = TextOnly(
        [
            sentinel_response(_src_without_reveal(body_src)),
            sentinel_response(_dst_with_reveal(body_dst)),
        ]
    )
    move_reveal(project, 2, 3, REVEAL, provider=provider, verify=False)
    assert REVEAL in project.read_chapter(3)[1]
    # Plain completions only: no tools were ever offered to the provider.
    assert all(not req.tools for req in provider.requests)


# ---------------------------------------------------------------------------
# U8: post-refactor verification
# ---------------------------------------------------------------------------


def review_json_response(issue: str) -> CompletionResponse:
    return text_response(
        json.dumps(
            {
                "findings": [
                    {
                        "severity": "major",
                        "category": "timeline",
                        "quote": "",
                        "issue": issue,
                        "suggestion": "check the merged seam",
                    }
                ],
                "summary": "continuity checked",
            }
        )
    )


def archivist_json_response() -> CompletionResponse:
    return text_response(
        json.dumps(
            {"summary": "merged chapter", "facts": [], "new_entities": [], "thread_updates": []}
        )
    )


def test_merge_with_verification_saves_report_and_stays_advisory(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    provider = ScriptedProvider(
        [review_json_response("timeline wobbles at the merge seam"), archivist_json_response()]
    )

    res = merge_chapters(project, 2, 3, verify=True, provider=provider)

    # Major MODEL findings do not fail the refactor (advisory only).
    assert not res.integrity_failed
    majors = [f for f in res.findings if f.severity == Severity.major]
    assert any("timeline wobbles" in f.issue for f in majors)
    assert res.report_path
    payload = json.loads(Path(res.report_path).read_text(encoding="utf-8"))
    assert payload["kind"] == "drafts-verify"
    assert payload["affected"] == [2]
    assert any("timeline wobbles" in f["issue"] for f in payload["findings"])

    actions = [e.action for e in Ledger(project.root).tail(50)]
    assert "drafts.refactor.merge" in actions
    assert "drafts.verify" in actions
    assert actions.index("drafts.refactor.merge") < actions.index("drafts.verify")


def test_no_verify_runs_integrity_only_and_constructs_no_provider(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])
    provider = ScriptedProvider([review_json_response("never consumed")])

    res = merge_chapters(project, 2, 3, verify=False, provider=provider)

    assert provider.requests == []  # no model calls at all
    assert not res.integrity_failed
    assert not any(e.action == "drafts.verify" for e in Ledger(project.root).tail(50))


def test_provider_error_mid_verification_degrades_to_note(tmp_path: Path):
    project = seed_project(tmp_path / "book", [1, 2, 3])

    res = merge_chapters(project, 2, 3, verify=True, provider=RaisingProvider())

    # Refactor result intact: merged chapter exists, exit governed by integrity.
    assert not res.integrity_failed
    _fm, merged = project.read_chapter(2)
    assert "Chapter 3 opens" in merged
    degraded = [n for n in res.notes if "skipped" in n] + [
        f.issue for f in res.findings if "pass failed" in f.issue
    ]
    assert degraded  # the failure was recorded, not raised


def test_config_verify_after_refactor_false_skips_model_checks(tmp_path: Path):
    import yaml

    project = seed_project(tmp_path / "book", [1, 2, 3])
    cfg_path = project.root / "stoner.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    data["archaeology"] = {"verify_after_refactor": False}
    cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    project = WritingProject(project.root)  # reload config
    provider = ScriptedProvider([review_json_response("never consumed")])

    merge_chapters(project, 2, 3, verify=True, provider=provider)
    assert provider.requests == []


def test_verify_after_orphaned_beats_exits_nonzero(tmp_path: Path, monkeypatch):
    from stoner.cli.main import app

    project = seed_project(tmp_path / "book", [1, 2, 3])
    monkeypatch.chdir(project.root)
    (project.root / "manuscript" / "ch-03.md").unlink()  # orphans its beats file

    result = runner.invoke(app, ["drafts", "verify"])
    assert result.exit_code == 1
    assert "orphan" in result.output


def test_drafts_verify_standalone_healthy_project(tmp_path: Path, monkeypatch):
    from stoner.cli.main import app

    project = seed_project(tmp_path / "book", [1, 2, 3])
    monkeypatch.chdir(project.root)

    result = runner.invoke(app, ["drafts", "verify"])
    assert result.exit_code == 0, result.output
    assert "integrity ok" in result.output
    assert any(e.action == "drafts.verify" for e in Ledger(project.root).tail(10))
