"""Tests for the Promise & Motif Ledger's measurement half (U4 deterministic
scans) and its advisory LLM layer (U5). No network: providers are scripted.

Calibration follows tests/test_slop.py's paired-fixture stance: assert
directional separation (an echoing ending beats an unrelated one) and exact
matrix cells for a seeded fixture, not brittle absolute magic numbers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.motifs import (
    mine_candidates,
    render,
    rhyme_overlap,
    save_rhyme,
    save_scan,
    scan_motifs,
)
from stoner.motifs.judge import judge_candidates, judge_rhyme
from stoner.motifs.scan import stem
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.types import CompletionRequest, CompletionResponse, Severity, Usage

# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


class FakeProvider(Provider):
    name = "fake"
    supports_tools = False

    def __init__(self, responses: list | Callable[[int], CompletionResponse]):
        self.responses = responses
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        item = self.responses(idx) if callable(self.responses) else self.responses[
            min(idx, len(self.responses) - 1)
        ]
        if isinstance(item, Exception):
            raise item
        return item


def _fenced(payload: dict) -> CompletionResponse:
    return CompletionResponse(
        text="```json\n" + json.dumps(payload) + "\n```",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def _mk_project(tmp_path: Path, chapters: dict[int, str]) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "book")
    scaffold_project(p, "book")
    for n, body in chapters.items():
        p.write_chapter(n, {"title": f"ch{n}"}, body)
    return p


RIVER_CHAPTERS = {
    1: "The river ran cold past the mill and the boy watched it go by slowly.",
    2: "The market was loud with sellers and the warm smell of bread everywhere.",
    3: "By the river again she waited, counting the slow brown water at dusk.",
    4: "The council met inside the hall to argue about the coming harvest tax.",
    5: "He returned to the river one last time before the deep winter closed in.",
}


# ---------------------------------------------------------------------------
# U4: stemmer
# ---------------------------------------------------------------------------


def test_stem_collapses_inflections():
    assert stem("rivers") == stem("river") == "river"
    assert stem("running") == "runn"  # crude, but stable
    assert stem("go") == "go"  # short words pass through


# ---------------------------------------------------------------------------
# U4: recurrence matrix
# ---------------------------------------------------------------------------


def test_recurrence_matrix_hits_expected_cells(tmp_path):
    project = _mk_project(tmp_path, RIVER_CHAPTERS)
    store = CanonStore(project)
    store.add_motif("mo1", "the river", anchors="river", meaning="time")
    report = scan_motifs(project, store)
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.per_chapter == {1: 1, 3: 1, 5: 1}
    assert row.chapters_hit == 3
    assert row.first == 1 and row.last == 5


def test_inflected_anchor_matches_via_stemming(tmp_path):
    project = _mk_project(tmp_path, {1: "Two rivers met below the ridge and joined into one."})
    store = CanonStore(project)
    store.add_motif("mo1", "river", anchors="river")
    report = scan_motifs(project, store)
    assert report.rows[0].per_chapter == {1: 1}


def test_empty_registry_yields_empty_matrix(tmp_path):
    project = _mk_project(tmp_path, RIVER_CHAPTERS)
    report = scan_motifs(project, CanonStore(project))
    assert report.rows == []
    assert report.chapters == [1, 2, 3, 4, 5]


def test_scan_is_deterministic(tmp_path):
    project = _mk_project(tmp_path, RIVER_CHAPTERS)
    store = CanonStore(project)
    store.add_motif("mo1", "the river", anchors="river; water", meaning="time")
    a = scan_motifs(project, store)
    b = scan_motifs(project, store)
    assert a.rows == b.rows  # byte-identical deterministic content


# ---------------------------------------------------------------------------
# U4: candidate mining
# ---------------------------------------------------------------------------

CANDIDATE_CHAPTERS = {
    1: "The cold iron gate stood shut. The silver moon rose over it. A lone crow watched.",
    2: "Again the cold iron gate barred her. The silver moon hung there. The wind moved on.",
    3: "She passed the cold iron gate at dusk; the silver moon lit her long way home.",
    4: "Nothing here repeated much at all, only plain and ordinary quiet sentences today.",
}


def test_candidate_mining_surfaces_and_excludes(tmp_path):
    project = _mk_project(tmp_path, CANDIDATE_CHAPTERS)
    store = CanonStore(project)
    # a registered anchor must exclude its covered grams from candidates
    store.add_motif("mo1", "the moon", anchors="silver moon", meaning="watching")
    report = mine_candidates(project, store, min_chapters=3, cap=12)
    grams = {c.gram for c in report.candidates}
    assert "cold iron gate" in grams
    assert not any("silver moon" in g for g in grams)  # covered by anchor
    # the surfaced candidate spans exactly the three seeded chapters
    gate = next(c for c in report.candidates if c.gram == "cold iron gate")
    assert gate.chapters == [1, 2, 3]


def test_candidate_mining_respects_min_chapters(tmp_path):
    project = _mk_project(tmp_path, CANDIDATE_CHAPTERS)
    store = CanonStore(project)
    report = mine_candidates(project, store, min_chapters=4, cap=12)
    assert all(c.gram != "cold iron gate" for c in report.candidates)


def test_candidate_mining_is_deterministic(tmp_path):
    project = _mk_project(tmp_path, CANDIDATE_CHAPTERS)
    store = CanonStore(project)
    a = mine_candidates(project, store, min_chapters=3)
    b = mine_candidates(project, store, min_chapters=3)
    assert [(c.gram, c.chapters, c.total) for c in a.candidates] == [
        (c.gram, c.chapters, c.total) for c in b.candidates
    ]


# ---------------------------------------------------------------------------
# U4: rhyme overlap
# ---------------------------------------------------------------------------

ECHO_CHAPTERS = {
    1: "The lighthouse burned at the edge of the black water. She remembered the crying gulls.",
    2: "In the city they forgot the sea entirely, lost in offices and traffic and endless noise.",
    3: "At the end she returned to the lighthouse and the black water and the crying gulls again.",
}
FLAT_CHAPTERS = {
    1: "The lighthouse burned at the edge of the black water. She remembered the crying gulls.",
    2: "In the city they forgot the sea entirely, lost in offices and traffic and endless noise.",
    3: "The accountant filed the quarterly taxes and drove home through the quiet suburban streets.",
}


def test_rhyme_echo_beats_unrelated_ending(tmp_path):
    echo = _mk_project(tmp_path / "e", ECHO_CHAPTERS)
    flat = _mk_project(tmp_path / "f", FLAT_CHAPTERS)
    er = rhyme_overlap(echo, CanonStore(echo), window=1)
    fr = rhyme_overlap(flat, CanonStore(flat), window=1)
    assert er.jaccard > fr.jaccard
    assert len(er.shared_distinctive) > len(fr.shared_distinctive)
    # distinctive terms are present in both windows and absent from the middle
    assert "lighthous" in er.shared_distinctive or "lighthouse" in er.shared_distinctive


def test_rhyme_motif_copresence_buckets(tmp_path):
    project = _mk_project(tmp_path, ECHO_CHAPTERS)
    store = CanonStore(project)
    store.add_motif("mo1", "the lighthouse", anchors="lighthouse", meaning="constancy")
    store.add_motif("mo2", "the city", anchors="city", meaning="forgetting")
    report = rhyme_overlap(project, store, window=1)
    assert "the lighthouse" in report.motifs_both  # opening + closing
    assert "the city" not in report.motifs_both  # only the middle


def test_rhyme_is_deterministic(tmp_path):
    project = _mk_project(tmp_path, ECHO_CHAPTERS)
    store = CanonStore(project)
    a = rhyme_overlap(project, store, window=1)
    b = rhyme_overlap(project, store, window=1)
    assert (a.jaccard, a.shared_distinctive) == (b.jaccard, b.shared_distinctive)


# ---------------------------------------------------------------------------
# U4: renderers
# ---------------------------------------------------------------------------


def test_all_renderers_produce_output(tmp_path, capsys):
    project = _mk_project(tmp_path, RIVER_CHAPTERS)
    store = CanonStore(project)
    store.add_motif("mo1", "the river", anchors="river", meaning="time")
    reports = [
        scan_motifs(project, store),
        mine_candidates(project, store, min_chapters=3),
        rhyme_overlap(project, store, window=1),
    ]
    for report in reports:
        for fmt in ("rich", "markdown", "json"):
            out = render(report, fmt)
            assert out.strip()
        with pytest.raises(ValueError):
            render(report, "yaml")
    # render is pure: it must not write to stdout itself
    assert capsys.readouterr().out == ""


def test_scan_json_carries_motifs_kind(tmp_path):
    project = _mk_project(tmp_path, RIVER_CHAPTERS)
    store = CanonStore(project)
    store.add_motif("mo1", "the river", anchors="river")
    payload = json.loads(render(scan_motifs(project, store), "json"))
    assert payload["kind"] == "motifs"


def test_save_helpers_write_kind_discriminated_reports(tmp_path):
    project = _mk_project(tmp_path, RIVER_CHAPTERS)
    store = CanonStore(project)
    store.add_motif("mo1", "the river", anchors="river")
    scan_rel = save_scan(project, scan_motifs(project, store))
    rhyme_rel = save_rhyme(project, rhyme_overlap(project, store, window=1))
    assert scan_rel.startswith(".stoner/reviews/motif-scan-")
    assert rhyme_rel.startswith(".stoner/reviews/motif-rhyme-")
    for rel in (scan_rel, rhyme_rel):
        assert json.loads(project.read(rel))["kind"] == "motifs"


# ---------------------------------------------------------------------------
# U5: advisory candidate triage
# ---------------------------------------------------------------------------


def test_judge_candidates_returns_findings_and_writes_nothing(tmp_path):
    project = _mk_project(tmp_path, CANDIDATE_CHAPTERS)
    store = CanonStore(project)
    report = mine_candidates(project, store, min_chapters=3)
    before = project.read("canon/motifs.md")

    provider = FakeProvider(
        [
            _fenced(
                {
                    "candidates": [
                        {
                            "gram": "cold iron gate",
                            "verdict": "promote",
                            "suggested_name": "the gate",
                            "meaning": "confinement",
                            "reason": "recurs at thresholds",
                        }
                    ]
                }
            )
        ]
    )
    findings, _usage = judge_candidates(project, report, store, provider=provider)
    assert findings
    assert all(f.source == "motif:candidates" for f in findings)
    assert findings[0].category == "promote"
    assert "cold iron gate" in findings[0].quote
    # advisory only: motifs.md is byte-identical
    assert project.read("canon/motifs.md") == before


def test_judge_candidates_garbage_degrades_to_one_info_finding(tmp_path):
    project = _mk_project(tmp_path, CANDIDATE_CHAPTERS)
    store = CanonStore(project)
    report = mine_candidates(project, store, min_chapters=3)
    provider = FakeProvider([CompletionResponse(text="not json at all", usage=Usage())])
    findings, _usage = judge_candidates(project, report, store, provider=provider)
    assert len(findings) == 1
    assert findings[0].severity == Severity.info


def test_judge_candidates_no_candidates_is_noop(tmp_path):
    project = _mk_project(tmp_path, {1: "plain quiet ordinary sentence here today."})
    store = CanonStore(project)
    report = mine_candidates(project, store, min_chapters=3)
    provider = FakeProvider([CompletionResponse(text="unused", usage=Usage())])
    findings, _usage = judge_candidates(project, report, store, provider=provider)
    assert findings == []
    assert provider.requests == []  # no model call when there is nothing to judge


# ---------------------------------------------------------------------------
# U5: advisory rhyme verdict
# ---------------------------------------------------------------------------


def test_judge_rhyme_maps_partial_verdict_with_pairs(tmp_path):
    project = _mk_project(tmp_path, ECHO_CHAPTERS)
    store = CanonStore(project)
    report = rhyme_overlap(project, store, window=1)
    provider = FakeProvider(
        [
            _fenced(
                {
                    "verdict": "PARTIAL",
                    "pairs": [
                        {"opening_quote": "the lighthouse burned", "closing_quote": "returned to the lighthouse", "note": "echo"},
                        {"opening_quote": "the crying gulls", "closing_quote": "the crying gulls again", "note": "return"},
                    ],
                }
            )
        ]
    )
    findings, _usage = judge_rhyme(project, report, store, window=1, provider=provider)
    assert all(f.source == "motif:rhyme" for f in findings)
    assert findings[0].category == "partial"
    assert findings[0].severity == Severity.minor
    pair_findings = [f for f in findings if f.category == "partial:pair"]
    assert len(pair_findings) == 2
    # each pair Finding carries both the opening and closing quote
    first = pair_findings[0]
    assert "the lighthouse burned" in first.quote
    assert "returned to the lighthouse" in first.suggestion


def test_judge_rhyme_prompt_excludes_middle_chapter(tmp_path):
    chapters = {
        1: "The lighthouse burned at the edge of the black water tonight.",
        2: "ZEBRAFISHSENTINEL marched through the middle of the book unseen.",
        3: "She returned to the lighthouse and the black water at the very end.",
    }
    project = _mk_project(tmp_path, chapters)
    store = CanonStore(project)
    report = rhyme_overlap(project, store, window=1)
    provider = FakeProvider([_fenced({"verdict": "RHYMES", "pairs": []})])
    judge_rhyme(project, report, store, window=1, provider=provider)
    user = provider.requests[0].messages[0].content
    assert "ZEBRAFISHSENTINEL" not in user  # middle chapter never enters the prompt
    assert "lighthouse" in user  # opening/closing windows do


def test_judge_rhyme_garbage_degrades_to_one_info_finding(tmp_path):
    project = _mk_project(tmp_path, ECHO_CHAPTERS)
    store = CanonStore(project)
    report = rhyme_overlap(project, store, window=1)
    provider = FakeProvider([CompletionResponse(text="no verdict here", usage=Usage())])
    findings, _usage = judge_rhyme(project, report, store, window=1, provider=provider)
    assert len(findings) == 1
    assert findings[0].severity == Severity.info
