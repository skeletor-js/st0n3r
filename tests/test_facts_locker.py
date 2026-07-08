"""Tests for the fact locker core + canon integration (U2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.facts.locker import (
    apply_facts,
    diff_facts,
    list_facts,
    parse_research_json,
)
from stoner.project import WritingProject

# The novella's checkable specific, used as fixture material.
HUMBOLDT_CLAIM = (
    "Humboldt Category II violations carry a $1,200 reinspection fee and a 60-day cure"
)


@pytest.fixture
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Test Book")
    scaffold_project(proj, "Test Book")
    return proj


@pytest.fixture
def store(project: WritingProject) -> CanonStore:
    return CanonStore(project)


def _candidate(**over):
    base = {
        "name": "Category II reinspection fee",
        "claim": HUMBOLDT_CLAIM,
        "source_url": "https://humboldtgov.org/code",
        "source_title": "Humboldt County Code",
        "quote": "Category II ... $1,200 reinspection fee",
        "tags": ["permits", "humboldt"],
        "confidence": "high",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# scaffold / canon integration
# ---------------------------------------------------------------------------


def test_scaffold_creates_facts_dir_and_template(project: WritingProject):
    assert (project.root / "canon/facts").is_dir()
    assert (project.root / "canon/facts/_template.md").exists()


def test_scaffold_is_idempotent_for_facts(project: WritingProject):
    project.write("canon/facts/_template.md", "# hand-edited\n")
    written = scaffold_project(project, "Test Book")
    assert "canon/facts/_template.md" not in written
    assert project.read("canon/facts/_template.md") == "# hand-edited\n"


def test_list_entries_fact_kind_excludes_template(store: CanonStore):
    store.upsert_fact("some-fact", {"name": "Some Fact", "claim": "a claim"})
    facts = store.list_entries(kind="fact")
    slugs = {e.slug for e in facts}
    assert "some-fact" in slugs
    assert "_template" not in slugs


# ---------------------------------------------------------------------------
# context_pack Facts section
# ---------------------------------------------------------------------------


def test_context_pack_includes_facts_line(store: CanonStore):
    apply_facts(store, [_candidate()], auto=True)
    pack = store.context_pack()
    assert "## Facts" in pack
    assert "$1,200 reinspection fee" in pack
    assert "humboldtgov.org" in pack


def test_context_pack_facts_section_drops_whole_under_tiny_budget(store: CanonStore):
    apply_facts(store, [_candidate()], auto=True)
    pack = store.context_pack(max_chars=200)
    # The Facts section is whole-or-nothing: it must not appear half-quoted.
    assert "## Facts" not in pack or HUMBOLDT_CLAIM in pack


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def test_parse_research_json_fenced():
    text = (
        "Here are the facts:\n```json\n"
        '{"facts": [{"name": "Fee", "claim": "c", "source_url": "https://x.org",'
        ' "quote": "q", "confidence": "high"}]}\n```'
    )
    facts = parse_research_json(text)
    assert len(facts) == 1
    assert facts[0]["name"] == "Fee"
    assert facts[0]["confidence"] == "high"


def test_parse_research_json_bare_list_and_garbage():
    assert parse_research_json('[{"name":"A","claim":"c","source_url":"https://x"}]')[0]["name"] == "A"
    assert parse_research_json("not json at all") == []
    assert parse_research_json("")[:] == []


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_new_slug_no_conflict(store: CanonStore):
    assert diff_facts([_candidate()], store) == []


def test_diff_same_claim_case_shift_no_conflict(store: CanonStore):
    apply_facts(store, [_candidate()], auto=True)
    shifted = _candidate(claim=HUMBOLDT_CLAIM.upper())
    assert diff_facts([shifted], store) == []


def test_diff_different_fee_is_conflict(store: CanonStore):
    apply_facts(store, [_candidate()], auto=True)
    contradiction = _candidate(
        claim="Humboldt Category II violations carry a $500 reinspection fee"
    )
    conflicts = diff_facts([contradiction], store)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.slug == "category-ii-reinspection-fee"
    assert "$1,200" in str(c.locker_value)
    assert "$500" in str(c.new_value)
    assert c.quote


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_apply_dry_run_touches_no_files(store: CanonStore, project: WritingProject):
    res = apply_facts(store, [_candidate()], auto=False)
    assert res.dry_run is True
    assert len(res.applied) == 1
    assert not (project.root / "canon/facts/category-ii-reinspection-fee.md").exists()


def test_apply_auto_writes_only_nonconflicting(store: CanonStore, project: WritingProject):
    apply_facts(store, [_candidate()], auto=True)
    contradiction = _candidate(
        claim="Humboldt Category II violations carry a $500 reinspection fee"
    )
    res = apply_facts(store, [contradiction], auto=True)
    assert len(res.conflicts) == 1
    assert res.applied == []
    # The original claim is untouched -- no auto-overwrite.
    entry = store.get_fact("category-ii-reinspection-fee")
    assert "$1,200" in entry.frontmatter["claim"]


def test_apply_preserves_hand_edited_body(store: CanonStore, project: WritingProject):
    apply_facts(store, [_candidate()], auto=True)
    rel = "canon/facts/category-ii-reinspection-fee.md"
    from stoner.project import join_frontmatter, split_frontmatter

    fm, _body = split_frontmatter(project.read(rel))
    project.write(rel, join_frontmatter(fm, "## Quotes\n\n> HAND EDITED\n\n## Notes\nmine\n"))
    # Re-apply the same slug's unchanged claim.
    apply_facts(store, [_candidate()], auto=True)
    assert "HAND EDITED" in project.read(rel)


def test_apply_drops_unsourced_candidate(store: CanonStore):
    res = apply_facts(store, [_candidate(source_url="")], auto=True)
    assert res.applied == []
    assert res.skipped_unsourced == 1
    assert list_facts(store) == []


def test_humboldt_conflict_scenario(store: CanonStore):
    # Seed with the $1,200 fee fact, then apply a contradictory $500 candidate.
    apply_facts(store, [_candidate()], auto=True)
    res = apply_facts(
        store,
        [_candidate(claim="Category II reinspection fee is $500")],
        auto=True,
    )
    assert len(res.conflicts) == 1
    assert res.conflicts[0].field == "claim"
