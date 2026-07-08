"""Tests for the canon package: store, scaffold, memory, archivist."""

from __future__ import annotations

from pathlib import Path

import pytest

from stoner.canon.archivist import (
    ArchivistError,
    apply_updates,
    diff_against_canon,
    extract_facts_prompt,
    parse_archivist_json,
)
from stoner.canon.memory import Memory
from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import PROMISE_KINDS, CanonError, CanonStore, MotifRow, slugify
from stoner.project import WritingProject

_LEGACY_THREADS = """# Threads

> Some hand-edited preamble prose that must survive verbatim.

| id | thread | opened_in | status | resolved_in | notes |
|----|--------|-----------|--------|-------------|-------|
| t1 | who killed the duke | ch-01 | open |  |  |

Trailing prose after the table, also verbatim.
"""


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "mybook", "My Book")
    scaffold_project(proj, "My Book")
    return proj


@pytest.fixture()
def store(project: WritingProject) -> CanonStore:
    return CanonStore(project)


# ---------------------------------------------------------------------------
# scaffold
# ---------------------------------------------------------------------------


def test_scaffold_writes_expected_files(project: WritingProject):
    for rel in [
        "canon/premise.md",
        "canon/style.md",
        "canon/timeline.md",
        "canon/threads.md",
        "canon/characters/_template.md",
        "canon/world/_template.md",
        "outline/outline.md",
        "outline/beats/ch-01.md",
    ]:
        assert (project.root / rel).exists(), rel
    assert "My Book" in project.read("canon/premise.md")


def test_scaffold_is_idempotent(project: WritingProject):
    project.write("canon/premise.md", "# hand-edited\n")
    written = scaffold_project(project, "My Book")
    assert "canon/premise.md" not in written
    assert project.read("canon/premise.md") == "# hand-edited\n"


# ---------------------------------------------------------------------------
# store: CRUD
# ---------------------------------------------------------------------------


def test_slugify():
    assert slugify("Aria Voss") == "aria-voss"
    assert slugify("  Weird!! Name??") == "weird-name"


def test_list_entries_excludes_templates(store: CanonStore):
    kinds = {e.kind for e in store.list_entries()}
    assert "character" not in kinds  # only _template.md exists so far
    assert "premise" in kinds
    assert "style" in kinds


def test_upsert_character_creates_and_merges(store: CanonStore):
    entry = store.upsert_character("aria-voss", {"age": 29, "role": "protagonist"})
    assert entry.frontmatter["age"] == 29
    assert entry.frontmatter["role"] == "protagonist"
    assert entry.frontmatter["status"] == "alive"  # default preserved

    # shallow-merge on second call; body preserved when not passed
    entry = store.upsert_character("aria-voss", {"age": 30}, body="## Voice\n\nDry wit.\n")
    assert entry.frontmatter["age"] == 30
    assert entry.frontmatter["role"] == "protagonist"  # untouched
    assert "Dry wit." in entry.body

    reloaded = store.get_character("aria-voss")
    assert reloaded is not None
    assert reloaded.frontmatter["age"] == 30
    assert "Dry wit." in reloaded.body


def test_upsert_world_defaults(store: CanonStore):
    entry = store.upsert_world("the-hollow", {"type": "faction"})
    assert entry.frontmatter["type"] == "faction"
    assert store.get_world("the-hollow") is not None


def test_find_character_by_name(store: CanonStore):
    store.upsert_character("aria-voss", {"name": "Aria Voss"})
    found = store.find_character_by_name("aria voss")
    assert found is not None
    assert found.slug == "aria-voss"
    assert store.find_character_by_name("nobody") is None


def test_search_case_insensitive(store: CanonStore):
    store.upsert_character("aria-voss", {"name": "Aria Voss"}, body="## Voice\n\nSpeaks in riddles.\n")
    hits = store.search("riddles")
    assert any(h.rel_path == "canon/characters/aria-voss.md" for h in hits)
    hits_upper = store.search("RIDDLES")
    assert hits_upper


# ---------------------------------------------------------------------------
# store: banned terms
# ---------------------------------------------------------------------------


def test_banned_terms_parses_seeded_yaml(store: CanonStore):
    words, phrases = store.banned_terms()
    assert "delve" in words
    assert any("couldn't help but" in p for p in phrases)


def test_banned_terms_tolerant_when_missing(project: WritingProject):
    project.write("canon/style.md", "# Style\n\nNo banned section here.\n")
    store = CanonStore(project)
    assert store.banned_terms() == ([], [])


def test_style_body_without_banned_strips_section(store: CanonStore):
    body = store.style_body_without_banned()
    assert "delve" not in body
    assert "## Voice" in body


# ---------------------------------------------------------------------------
# store: threads round trip
# ---------------------------------------------------------------------------


def test_threads_round_trip(store: CanonStore):
    assert store.threads() == []
    store.add_thread("t1", "who killed the duke", opened_in="ch-01")
    rows = store.threads()
    assert len(rows) == 1
    assert rows[0].id == "t1"
    assert rows[0].status == "open"

    updated = store.update_thread("t1", status="resolved", resolved_in="ch-09", notes="it was the butler")
    assert updated.status == "resolved"
    rows = store.threads()
    assert rows[0].status == "resolved"
    assert rows[0].resolved_in == "ch-09"

    with pytest.raises(CanonError):
        store.update_thread("does-not-exist", status="open")


def test_add_thread_duplicate_id_raises(store: CanonStore):
    store.add_thread("t1", "a thread")
    with pytest.raises(CanonError):
        store.add_thread("t1", "another thread")


# ---------------------------------------------------------------------------
# store: timeline
# ---------------------------------------------------------------------------


def test_timeline_round_trip(store: CanonStore):
    assert store.timeline_rows() == []
    store.add_timeline_row("ch-01", "The duke is found dead", chapters="1", characters="Aria Voss")
    rows = store.timeline_rows()
    assert len(rows) == 1
    assert rows[0].event == "The duke is found dead"


# ---------------------------------------------------------------------------
# store: context_pack
# ---------------------------------------------------------------------------


def test_context_pack_includes_priority_sections(store: CanonStore):
    store.project.write("canon/premise.md", "# Premise\n\nA duke is murdered.\n")
    store.upsert_character("aria-voss", {"name": "Aria Voss", "first_appearance": "ch-01"}, body="## Voice\n\nDry.\n")
    store.add_thread("t1", "who killed the duke", opened_in="ch-01")

    pack = store.context_pack(max_chars=12000)
    assert "Premise" in pack
    assert "murdered" in pack
    assert "Style" in pack
    assert "Open Threads" in pack
    assert "Aria Voss" in pack


def test_context_pack_respects_budget(store: CanonStore):
    store.project.write("canon/premise.md", "# Premise\n\n" + ("x" * 5000) + "\n")
    for i in range(20):
        store.upsert_character(f"char-{i}", {"name": f"Char {i}", "first_appearance": f"ch-{i:02d}"})

    pack = store.context_pack(max_chars=6000)
    assert len(pack) <= 6000 + 200  # small slop allowed for truncation markers
    assert "Premise" in pack


def test_context_pack_prioritizes_recent_characters(store: CanonStore):
    store.upsert_character("old-char", {"name": "Old Char", "first_appearance": "ch-01"})
    store.upsert_character("new-char", {"name": "New Char", "first_appearance": "ch-20"})
    pack = store.context_pack(max_chars=12000)
    # both fit at this budget, but recent should appear before older
    assert pack.index("New Char") < pack.index("Old Char")


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def test_memory_chapter_summary_round_trip(project: WritingProject):
    mem = Memory(project)
    assert mem.get_chapter_summary(1) is None
    mem.set_chapter_summary(1, "Aria finds the body.", pov="Aria", words=1500)
    assert mem.get_chapter_summary(1) == "Aria finds the body."
    chapter = mem.get_chapter(1)
    assert chapter is not None
    assert chapter["pov"] == "Aria"
    assert chapter["words"] == 1500


def test_memory_rebuild_book_so_far(project: WritingProject):
    mem = Memory(project)
    mem.set_chapter_summary(1, "Chapter one happens.")
    mem.set_chapter_summary(2, "Chapter two happens.")
    book = mem.rebuild_book_so_far()
    assert "Ch 1" in book
    assert "Ch 2" in book
    assert mem.book_so_far == book


def test_memory_rebuild_book_so_far_caps_length(project: WritingProject):
    mem = Memory(project)
    for i in range(1, 30):
        mem.set_chapter_summary(i, f"Summary of chapter {i}. " * 20)
    book = mem.rebuild_book_so_far(max_chars=500)
    assert len(book) <= 500
    # most recent chapter should have survived the cap
    assert "Ch 29" in book


def test_memory_context_for_chapter_windows(project: WritingProject):
    mem = Memory(project)
    for i in range(1, 8):
        mem.set_chapter_summary(i, f"Full summary text for chapter {i}.", pov="Aria")
    mem.rebuild_book_so_far()

    ctx = mem.context_for_chapter(7)
    assert "Book So Far" in ctx
    assert "Recent Chapters" in ctx
    # chapters 4,5,6 should be full summaries
    assert "Full summary text for chapter 6." in ctx
    assert "Chapter 6" in ctx
    # chapter 1-3 should be compressed, not full-quoted twice as headings
    assert "Earlier Chapters" in ctx
    assert "Ch 1:" in ctx


# ---------------------------------------------------------------------------
# archivist: prompt + parsing
# ---------------------------------------------------------------------------


def test_extract_facts_prompt_includes_context(store: CanonStore):
    prompt = extract_facts_prompt("Aria walked into the room.", "CANON DIGEST HERE")
    assert "CANON DIGEST HERE" in prompt
    assert "Aria walked into the room." in prompt
    assert "STRICT JSON" in prompt


def test_parse_archivist_json_clean():
    text = '{"summary": "s", "facts": [], "new_entities": [], "thread_updates": []}'
    parsed = parse_archivist_json(text)
    assert parsed["summary"] == "s"


def test_parse_archivist_json_fenced():
    text = 'Here is the result:\n```json\n{"summary": "s", "facts": []}\n```\nThanks!'
    parsed = parse_archivist_json(text)
    assert parsed["summary"] == "s"
    assert parsed["new_entities"] == []  # defaulted


def test_parse_archivist_json_dirty_trailing_text():
    text = 'Sure, here you go: {"summary": "ok", "facts": [{"entity": "x", "kind": "character", "field": "age", "value": "10", "quote": "q"}]} Let me know if you need more.'
    parsed = parse_archivist_json(text)
    assert parsed["summary"] == "ok"
    assert parsed["facts"][0]["entity"] == "x"


def test_parse_archivist_json_garbage_raises():
    with pytest.raises(ArchivistError):
        parse_archivist_json("no json anywhere in this string")


# ---------------------------------------------------------------------------
# archivist: conflict detection
# ---------------------------------------------------------------------------


def test_diff_detects_age_change(store: CanonStore):
    store.upsert_character("aria-voss", {"name": "Aria Voss", "age": 29})
    facts = [{"entity": "Aria Voss", "kind": "character", "field": "age", "value": 45, "quote": "q"}]
    conflicts = diff_against_canon(facts, store)
    assert len(conflicts) == 1
    assert conflicts[0].field == "age"
    assert conflicts[0].canon_value == 29


def test_diff_age_within_tolerance_no_conflict(store: CanonStore):
    store.upsert_character("aria-voss", {"name": "Aria Voss", "age": 29})
    facts = [{"entity": "Aria Voss", "kind": "character", "field": "age", "value": 30, "quote": "q"}]
    conflicts = diff_against_canon(facts, store)
    assert conflicts == []


def test_diff_detects_eye_color_change(store: CanonStore):
    store.upsert_character("aria-voss", {"name": "Aria Voss", "appearance": {}})
    store.upsert_character("aria-voss", {"eyes": "green"})
    facts = [{"entity": "Aria Voss", "kind": "character", "field": "eyes", "value": "blue", "quote": "q"}]
    conflicts = diff_against_canon(facts, store)
    assert len(conflicts) == 1
    assert conflicts[0].canon_value == "green"


def test_diff_no_false_positive_same_value_different_case(store: CanonStore):
    store.upsert_character("aria-voss", {"name": "Aria Voss", "eyes": "Green"})
    facts = [{"entity": "Aria Voss", "kind": "character", "field": "eyes", "value": "green", "quote": "q"}]
    conflicts = diff_against_canon(facts, store)
    assert conflicts == []


def test_diff_no_conflict_for_new_field(store: CanonStore):
    store.upsert_character("aria-voss", {"name": "Aria Voss"})
    facts = [{"entity": "Aria Voss", "kind": "character", "field": "eyes", "value": "green", "quote": "q"}]
    conflicts = diff_against_canon(facts, store)
    assert conflicts == []  # canon had no prior value for eyes


def test_diff_ignores_unknown_entity(store: CanonStore):
    facts = [{"entity": "Nobody", "kind": "character", "field": "age", "value": 10, "quote": "q"}]
    assert diff_against_canon(facts, store) == []


# ---------------------------------------------------------------------------
# archivist: apply_updates
# ---------------------------------------------------------------------------


def test_apply_updates_dry_run_does_not_write(store: CanonStore, project: WritingProject):
    store.upsert_character("aria-voss", {"name": "Aria Voss", "age": 29})
    mem = Memory(project)
    parsed = {
        "summary": "Aria turns 30.",
        "facts": [{"entity": "Aria Voss", "kind": "character", "field": "age", "value": 30, "quote": "q"}],
        "new_entities": [],
        "thread_updates": [],
    }
    result = apply_updates(store, mem, parsed, chapter_number=3, auto=False)
    assert result.dry_run is True
    assert len(result.applied_facts) == 1
    assert store.get_character("aria-voss").frontmatter["age"] == 29  # unchanged
    assert mem.get_chapter_summary(3) is None
    assert result.summary_saved is False


def test_apply_updates_auto_merges_non_conflicting(store: CanonStore, project: WritingProject):
    store.upsert_character("aria-voss", {"name": "Aria Voss"})
    mem = Memory(project)
    parsed = {
        "summary": "Aria's eyes are described as green.",
        "facts": [{"entity": "Aria Voss", "kind": "character", "field": "eyes", "value": "green", "quote": "q"}],
        "new_entities": [{"name": "The Hollow", "kind": "world", "reason": "new location"}],
        "thread_updates": [],
    }
    result = apply_updates(store, mem, parsed, chapter_number=1, auto=True)
    assert result.dry_run is False
    assert store.get_character("aria-voss").frontmatter["eyes"] == "green"
    assert mem.get_chapter_summary(1) == "Aria's eyes are described as green."
    assert result.summary_saved is True
    assert result.new_entities[0]["name"] == "The Hollow"
    # new entity is surfaced for review, not silently created
    assert store.get_world("the-hollow") is None


def test_apply_updates_never_overwrites_conflicts(store: CanonStore, project: WritingProject):
    store.upsert_character("aria-voss", {"name": "Aria Voss", "age": 29})
    mem = Memory(project)
    parsed = {
        "summary": "s",
        "facts": [{"entity": "Aria Voss", "kind": "character", "field": "age", "value": 45, "quote": "q"}],
        "new_entities": [],
        "thread_updates": [],
    }
    result = apply_updates(store, mem, parsed, chapter_number=2, auto=True)
    assert len(result.conflicts) == 1
    assert result.applied_facts == []
    assert store.get_character("aria-voss").frontmatter["age"] == 29


def test_apply_updates_thread_updates(store: CanonStore, project: WritingProject):
    store.add_thread("t1", "who killed the duke", opened_in="ch-01")
    mem = Memory(project)
    parsed = {
        "summary": "The mystery is solved.",
        "facts": [],
        "new_entities": [],
        "thread_updates": [{"id": "t1", "status": "resolved", "note": "the butler did it"}],
    }
    result = apply_updates(store, mem, parsed, chapter_number=9, auto=True)
    assert result.thread_updates[0]["applied"] is True
    rows = store.threads()
    assert rows[0].status == "resolved"


def test_apply_updates_unknown_thread_id_not_applied(store: CanonStore, project: WritingProject):
    mem = Memory(project)
    parsed = {
        "summary": "s",
        "facts": [],
        "new_entities": [],
        "thread_updates": [{"id": "ghost", "status": "resolved", "note": "n"}],
    }
    result = apply_updates(store, mem, parsed, chapter_number=1, auto=True)
    assert result.thread_updates[0]["applied"] is False


# ---------------------------------------------------------------------------
# U1: promise-typed thread rows
# ---------------------------------------------------------------------------


def test_legacy_six_column_threads_read_with_empty_kind(store: CanonStore):
    store.project.write("canon/threads.md", _LEGACY_THREADS)
    rows = store.threads()
    assert len(rows) == 1
    assert rows[0].id == "t1"
    assert rows[0].kind == ""  # padded, not dropped


def test_plant_promise_migrates_header_and_preserves_prose(store: CanonStore):
    store.project.write("canon/threads.md", _LEGACY_THREADS)
    row = store.plant_promise("p1", "will the gun go off", "threat", opened_in="ch-02")
    assert row.kind == "threat"

    raw = store.project.read("canon/threads.md")
    # hand-edited prefix/suffix prose round-trips verbatim
    assert "hand-edited preamble prose that must survive verbatim" in raw
    assert "Trailing prose after the table, also verbatim." in raw
    # header migrated to include the kind column
    header_line = next(line for line in raw.splitlines() if line.strip().startswith("| id"))
    assert "kind" in header_line

    rows = {r.id: r for r in store.threads()}
    assert rows["t1"].thread == "who killed the duke"  # legacy row intact
    assert rows["t1"].kind == ""
    assert rows["p1"].kind == "threat"
    assert rows["p1"].status == "open"


def test_promises_view_filters_by_kind(store: CanonStore):
    store.add_thread("plain", "a plain plot thread")
    store.plant_promise("m1", "who is the ghost", "mystery", opened_in="ch-01")
    promises = store.promises()
    assert [p.id for p in promises] == ["m1"]


def test_payoff_promise_sets_resolved(store: CanonStore):
    store.plant_promise("m1", "who is the ghost", "mystery", opened_in="ch-01")
    row = store.payoff_promise("m1", "ch-09", notes="it was the caretaker")
    assert row.status == "resolved"
    assert row.resolved_in == "ch-09"
    assert store.promises()[0].notes == "it was the caretaker"


def test_plant_promise_invalid_kind_raises(store: CanonStore):
    with pytest.raises(CanonError):
        store.plant_promise("x", "bad", "prophecy", opened_in="ch-01")


def test_plant_promise_duplicate_id_raises(store: CanonStore):
    store.plant_promise("m1", "who is the ghost", "mystery")
    with pytest.raises(CanonError):
        store.plant_promise("m1", "another", "want")


def test_promise_kinds_are_the_four(store: CanonStore):
    assert set(PROMISE_KINDS) == {"mystery", "threat", "want", "image"}


def test_context_pack_open_threads_unchanged_by_kind(store: CanonStore):
    store.add_thread("t1", "who killed the duke", opened_in="ch-01")
    pack = store.context_pack(max_chars=12000)
    assert "Open Threads" in pack
    assert "(t1) who killed the duke -- opened ch-01" in pack


# ---------------------------------------------------------------------------
# U2: archivist plant/payoff extraction
# ---------------------------------------------------------------------------


def _parsed(**over):
    base = {"summary": "", "facts": [], "new_entities": [], "thread_updates": [], "planted_threads": []}
    base.update(over)
    return base


def test_apply_plant_appends_once_and_is_idempotent(store: CanonStore, project: WritingProject):
    mem = Memory(project)
    parsed = _parsed(
        planted_threads=[{"id": "p1", "thread": "the locked box", "kind": "image", "quote": "q"}]
    )
    r1 = apply_updates(store, mem, parsed, chapter_number=3, auto=True)
    assert r1.planted_threads[0]["applied"] is True
    assert r1.planted_threads[0]["opened_in"] == "ch-03"
    rows = store.promises()
    assert len(rows) == 1
    assert rows[0].kind == "image"
    assert rows[0].opened_in == "ch-03"

    # second apply of the same plant is a no-op (idempotent on redraft)
    r2 = apply_updates(store, mem, parsed, chapter_number=3, auto=True)
    assert r2.planted_threads[0]["applied"] is False
    assert r2.planted_threads[0]["reason"] == "already planted"
    assert len(store.promises()) == 1


def test_apply_plant_id_collision_surfaced_unapplied(store: CanonStore, project: WritingProject):
    store.add_thread("p1", "an existing thread", opened_in="ch-01")
    mem = Memory(project)
    parsed = _parsed(
        planted_threads=[{"id": "p1", "thread": "a totally different promise", "kind": "want", "quote": "q"}]
    )
    result = apply_updates(store, mem, parsed, chapter_number=2, auto=True)
    assert result.planted_threads[0]["applied"] is False
    assert "collide" in result.planted_threads[0]["reason"]
    # existing thread untouched
    assert store.threads()[0].thread == "an existing thread"


def test_apply_plant_invalid_kind_surfaced(store: CanonStore, project: WritingProject):
    mem = Memory(project)
    parsed = _parsed(
        planted_threads=[{"id": "p1", "thread": "x", "kind": "prophecy", "quote": "q"}]
    )
    result = apply_updates(store, mem, parsed, chapter_number=1, auto=True)
    assert result.planted_threads[0]["applied"] is False
    assert store.promises() == []


def test_apply_resolve_stamps_resolved_in(store: CanonStore, project: WritingProject):
    store.plant_promise("m1", "who is the ghost", "mystery", opened_in="ch-01")
    mem = Memory(project)
    parsed = _parsed(thread_updates=[{"id": "m1", "status": "resolved", "note": "solved"}])
    apply_updates(store, mem, parsed, chapter_number=9, auto=True)
    row = store.promises()[0]
    assert row.status == "resolved"
    assert row.resolved_in == "ch-09"


def test_apply_resolve_does_not_overwrite_prior_payoff(store: CanonStore, project: WritingProject):
    store.plant_promise("m1", "who is the ghost", "mystery", opened_in="ch-01")
    store.payoff_promise("m1", "ch-05")
    mem = Memory(project)
    parsed = _parsed(thread_updates=[{"id": "m1", "status": "resolved", "note": "again"}])
    result = apply_updates(store, mem, parsed, chapter_number=9, auto=True)
    assert result.thread_updates[0]["applied"] is False
    assert "already resolved in ch-05" in result.thread_updates[0]["reason"]
    assert store.promises()[0].resolved_in == "ch-05"  # not overwritten


def test_legacy_archivist_json_without_planted_threads_applies(store: CanonStore, project: WritingProject):
    mem = Memory(project)
    parsed = parse_archivist_json(
        '{"summary": "s", "facts": [], "new_entities": [], "thread_updates": []}'
    )
    result = apply_updates(store, mem, parsed, chapter_number=1, auto=True)
    assert result.planted_threads == []


def test_apply_plant_dry_run_touches_nothing(store: CanonStore, project: WritingProject):
    mem = Memory(project)
    parsed = _parsed(
        planted_threads=[{"id": "p1", "thread": "the locked box", "kind": "image", "quote": "q"}]
    )
    result = apply_updates(store, mem, parsed, chapter_number=3, auto=False)
    assert result.planted_threads[0]["applied"] is False
    assert store.promises() == []  # nothing written


# ---------------------------------------------------------------------------
# U3: motif registry
# ---------------------------------------------------------------------------


def test_scaffold_writes_motifs_once(project: WritingProject):
    assert (project.root / "canon/motifs.md").exists()
    project.write("canon/motifs.md", "# hand-edited\n")
    written = scaffold_project(project, "My Book")
    assert "canon/motifs.md" not in written
    assert project.read("canon/motifs.md") == "# hand-edited\n"


def test_motif_round_trip_with_semicolon_anchors(store: CanonStore):
    assert store.motifs() == []
    store.add_motif("mo1", "the river", anchors="river; the current; downstream", meaning="time")
    rows = store.motifs()
    assert len(rows) == 1
    assert rows[0].motif == "the river"
    assert rows[0].anchor_list() == ["river", "the current", "downstream"]
    assert rows[0].meaning == "time"

    updated = store.update_motif("mo1", meaning="fate")
    assert updated.meaning == "fate"
    assert store.motifs()[0].meaning == "fate"

    with pytest.raises(CanonError):
        store.update_motif("nope", meaning="x")
    with pytest.raises(CanonError):
        store.add_motif("mo1", "dup")


def test_motif_anchor_list_edge_cases():
    assert MotifRow("i", "m", anchors="").anchor_list() == []
    assert MotifRow("i", "m", anchors="solo").anchor_list() == ["solo"]
    assert MotifRow("i", "m", anchors=" a ; ; b ").anchor_list() == ["a", "b"]


def test_context_pack_includes_motifs_after_threads(store: CanonStore):
    store.add_thread("t1", "who killed the duke", opened_in="ch-01")
    store.add_motif("mo1", "the river", anchors="river; current", meaning="the passage of time")
    pack = store.context_pack(max_chars=12000)
    assert "Motifs" in pack
    assert "the river: the passage of time" in pack
    assert "anchors: river; current" in pack
    assert pack.index("Open Threads") < pack.index("Motifs")


def test_context_pack_omits_motifs_when_empty(store: CanonStore):
    store.add_thread("t1", "a thread", opened_in="ch-01")
    pack = store.context_pack(max_chars=12000)
    assert "Motifs" not in pack


def test_context_pack_drops_motifs_section_whole_under_tight_budget(store: CanonStore):
    store.project.write("canon/premise.md", "# Premise\n\n" + ("x" * 4000) + "\n")
    store.add_thread("t1", "a thread", opened_in="ch-01")
    store.add_motif("mo1", "the river", anchors="river", meaning="time")
    pack = store.context_pack(max_chars=200)
    # too tight for anything past premise-truncation; motifs never appear mangled
    assert "the river" not in pack
