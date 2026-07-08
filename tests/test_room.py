"""Tests for the Writers' Room core: roster/config (U1), notebooks (U2),
re-location (U4), and the session engine (U5). All offline: every model call
goes through a scripted FakeProvider (tests/test_pacing_judge.py pattern)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from stoner.canon.memory import Memory
from stoner.canon.scaffold import scaffold_project
from stoner.config import EditorSpec, RoomConfig, StonerConfig
from stoner.ledger import Ledger
from stoner.project import WritingProject
from stoner.providers.base import Provider, ProviderError
from stoner.room.notebook import Notebook
from stoner.room.relocate import relocate_items
from stoner.room.roster import load_roster, slugify
from stoner.room.session import run_room_session
from stoner.types import CompletionRequest, CompletionResponse, Usage

BODY = (
    "The harbor bell rang twice before Mara reached the quay. She counted the\n"
    "crates herself, twice, and the count came up short both times.\n\n"
    "Holt watched her from the gangway and said nothing, which was itself an\n"
    "answer of a kind she had learned to read.\n"
)


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, responses: list | Callable[[int, CompletionRequest], CompletionResponse]):
        self.responses = responses
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        if callable(self.responses):
            item = self.responses(idx, req)
        else:
            item = self.responses[min(idx, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _json_resp(payload: dict, tokens: int = 10) -> CompletionResponse:
    return CompletionResponse(
        text=json.dumps(payload), usage=Usage(input_tokens=tokens, output_tokens=5)
    )


def _take_resp(quote: str = "The harbor bell rang twice") -> CompletionResponse:
    return _json_resp(
        {
            "findings": [
                {
                    "severity": "major",
                    "category": "test",
                    "quote": quote,
                    "issue": "an issue",
                    "suggestion": "a fix",
                }
            ],
            "summary": "a take summary",
        }
    )


def _crossexam_resp(**overrides) -> CompletionResponse:
    payload = {
        "agreements": [],
        "disagreements": [],
        "priority_rank": [],
        "comment_responses": [],
        "notebook_note": "the running opinion",
    }
    payload.update(overrides)
    return _json_resp(payload)


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "mybook", "My Book")
    scaffold_project(proj, "My Book")
    proj.write_chapter(1, {"title": "Arrival", "status": "draft"}, BODY)
    return proj


def _small_roster_project(project: WritingProject) -> WritingProject:
    """Rewrite stoner.yaml with a 2-editor, 1-pass-each roster (4 calls/session)."""
    cfg = yaml.safe_load((project.root / "stoner.yaml").read_text(encoding="utf-8"))
    cfg["room"] = {
        "editors": [
            {"name": "Dev Editor", "persona": "You are dev.", "passes": ["pacing"]},
            {"name": "Line Editor", "persona": "You are line.", "passes": ["line"]},
        ]
    }
    (project.root / "stoner.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return WritingProject(project.root)


# ---------------------------------------------------------------------------
# U1: config, roster, scaffolding
# ---------------------------------------------------------------------------


def test_default_config_yields_four_editors_with_distinct_slugs():
    cfg = StonerConfig()
    roster = load_roster(cfg.room)
    assert len(roster) == 4
    slugs = [e.slug for e in roster]
    assert len(set(slugs)) == 4
    assert all(e.passes for e in roster)
    assert "developmental-editor" in slugs
    assert "first-reader" in slugs


def test_room_config_override_round_trips(project: WritingProject):
    proj = _small_roster_project(project)
    roster = load_roster(proj.config.room)
    assert [e.slug for e in roster] == ["dev-editor", "line-editor"]
    assert roster[0].passes == ["pacing"]
    # dump/reload keeps the roster
    reloaded = StonerConfig.model_validate(yaml.safe_load(proj.config.dump_yaml()))
    assert [e.name for e in reloaded.room.editors] == ["Dev Editor", "Line Editor"]


def test_unknown_pass_survives_config_load_and_is_soft():
    cfg = RoomConfig(
        editors=[EditorSpec(name="Odd One", passes=["line", "no-such-pass"])]
    )
    roster = load_roster(cfg)
    assert roster[0].passes == ["line"]
    assert roster[0].unknown_passes == ["no-such-pass"]


def test_create_scaffolds_room_dirs(project: WritingProject):
    for rel in (
        ".stoner/room",
        ".stoner/room/notebooks",
        ".stoner/room/sessions",
        ".stoner/room/comments",
    ):
        assert (project.root / rel).is_dir(), rel


def test_slugify_collisions_disambiguate():
    cfg = RoomConfig(
        editors=[EditorSpec(name="The Editor"), EditorSpec(name="the editor!")]
    )
    roster = load_roster(cfg)
    assert [e.slug for e in roster] == ["the-editor", "the-editor-2"]
    assert slugify("  !! ") == "editor"


# ---------------------------------------------------------------------------
# U2: notebook store
# ---------------------------------------------------------------------------


def test_notebook_upsert_round_trips(project: WritingProject):
    nb = Notebook(project, "line-editor")
    nb.upsert_item("f_1", chapter=1, quote="the count came up short", issue="repetition",
                   severity="minor", pass_name="line", session_id="s1")
    again = Notebook(project, "line-editor")
    items = again.items()
    assert len(items) == 1
    assert items[0]["id"] == "f_1"
    assert items[0]["status"] == "open"
    assert items[0]["first_session"] == "s1"


def test_notebook_escalation_increments_across_updates(project: WritingProject):
    nb = Notebook(project, "line-editor")
    nb.upsert_item("f_1", 1, "q", "issue", "minor", "line", "s1")
    nb.mark_persisting("f_1", "s2")
    nb.mark_persisting("f_1", "s3")
    item = nb.get("f_1")
    assert item["escalations"] == 2
    assert item["status"] == "persisting"
    assert item["last_seen_session"] == "s3"


def test_notebook_digest_respects_cap_and_recency(project: WritingProject):
    cfg = RoomConfig(digest_chars=200)
    nb = Notebook(project, "line-editor", cfg)
    for i in range(20):
        nb.upsert_item(f"f_{i}", 1, f"quote {i}", f"issue number {i} with some length to it",
                       "minor", "line", f"s{i:03d}")
    digest = nb.digest(chapter=1)
    assert len(digest) <= 200
    # most recent first: the newest item appears, the oldest does not
    assert "issue number 19" in digest
    assert "issue number 0 " not in digest


def test_notebook_digest_excludes_other_chapters(project: WritingProject):
    nb = Notebook(project, "line-editor")
    nb.upsert_item("f_1", 1, "q1", "chapter one issue", "minor", "line", "s1")
    nb.upsert_item("f_2", 2, "q2", "chapter two issue", "minor", "line", "s1")
    digest = nb.digest(chapter=1)
    assert "chapter one issue" in digest
    assert "chapter two issue" not in digest


def test_notebook_eviction_prefers_dropping_resolved(project: WritingProject):
    cfg = RoomConfig(max_open_items=50, max_resolved_items=2)
    nb = Notebook(project, "line-editor", cfg)
    for i in range(5):
        nb.upsert_item(f"f_r{i}", 1, "q", "resolved issue", "minor", "line", f"s{i:03d}")
        nb.mark_status(f"f_r{i}", "resolved")
    for i in range(3):
        nb.upsert_item(f"f_o{i}", 1, "q", "open issue", "minor", "line", f"s{i:03d}")
    dropped = nb.evict()
    assert len(dropped) == 3
    assert all(d.startswith("f_r") for d in dropped)
    remaining = {it["id"] for it in nb.items()}
    assert {"f_o0", "f_o1", "f_o2"} <= remaining
    assert "[evicted" in nb.opinion


def test_notebook_corrupt_json_degrades_to_empty(project: WritingProject):
    path = project.root / ".stoner" / "room" / "notebooks" / "line-editor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    nb = Notebook(project, "line-editor")
    assert nb.items() == []
    assert nb.opinion == ""
    nb.upsert_item("f_1", 1, "q", "issue", "minor", "line", "s1")  # writable again
    assert len(nb.items()) == 1


def test_notebook_opinion_cap(project: WritingProject):
    cfg = RoomConfig(opinion_cap_chars=50)
    nb = Notebook(project, "line-editor", cfg)
    nb.set_opinion("x" * 500)
    assert len(nb.opinion) <= 50


def test_reconcile_dismissals_silences_item(project: WritingProject):
    nb = Notebook(project, "line-editor")
    nb.upsert_item("f_1", 1, "the count came up short", "flagged thing", "minor", "line", "s1")
    record = {
        "id": "ch-01-100",
        "findings": [
            {"id": "f_1", "source": "room:line-editor:line", "status": "dismissed"}
        ],
    }
    changed = nb.reconcile_dismissals([record])
    assert changed == 1
    assert nb.get("f_1")["status"] == "dismissed"
    # dismissed items stop tracking: no re-location, no digest, no escalation
    assert nb.tracked_items(1) == []
    assert "flagged thing" not in nb.digest(chapter=1)
    nb.mark_persisting("f_1", "s2")  # relocation never sees it, but even direct
    # calls leave dismissal reconcile idempotent on a second pass
    assert nb.reconcile_dismissals([record]) in (0, 1)


def test_reconcile_ignores_other_editors_findings(project: WritingProject):
    nb = Notebook(project, "line-editor")
    nb.upsert_item("f_1", 1, "q", "issue", "minor", "line", "s1")
    record = {"findings": [{"id": "f_1", "source": "room:first-reader:grade", "status": "dismissed"}]}
    assert nb.reconcile_dismissals([record]) == 0
    assert nb.get("f_1")["status"] == "open"


def test_notebook_stays_capped_after_thirty_session_churn(project: WritingProject):
    """R2: 30 sessions of churn never push the notebook past its caps."""
    cfg = RoomConfig(max_open_items=10, max_resolved_items=5, opinion_cap_chars=300)
    nb = Notebook(project, "line-editor", cfg)
    for session in range(30):
        sid = f"s{session:03d}"
        for i in range(3):  # three new flags per session
            nb.upsert_item(f"f_{session}_{i}", chapter=(session % 4) + 1,
                           quote=f"quote {session}-{i}", issue=f"issue {session}-{i}",
                           severity="minor" if i else "major", pass_name="line", session_id=sid)
        if session % 2 == 0 and session > 0:  # resolve one old flag every other session
            nb.mark_status(f"f_{session - 1}_0", "resolved", sid)
        nb.set_opinion(f"opinion after session {session} " * 20)
        nb.evict()
    data = json.loads(
        (project.root / ".stoner" / "room" / "notebooks" / "line-editor.json").read_text()
    )
    active = [it for it in data["items"] if it["status"] in ("open", "persisting", "unlocatable")]
    resolved = [it for it in data["items"] if it["status"] == "resolved"]
    assert len(active) <= 10
    assert len(resolved) <= 5
    assert len(data["opinion"]) <= 300


# ---------------------------------------------------------------------------
# U4: re-location
# ---------------------------------------------------------------------------


def _item(item_id: str, quote: str) -> dict:
    return {"id": item_id, "quote": quote, "issue": "an issue"}


def test_relocate_exact_match_is_persisting(project: WritingProject):
    provider = FakeProvider([])
    outcomes, usage = relocate_items(
        BODY, [_item("f_1", "the count came up short both times")], project, provider=provider
    )
    assert outcomes[0].outcome == "persisting"
    assert outcomes[0].tier == "exact"
    span = outcomes[0].span
    assert BODY[span.start : span.end] == "the count came up short both times"
    assert provider.requests == []  # no model call: cost discipline (R17)
    assert usage.input_tokens == 0


def test_relocate_whitespace_drift_via_tier2(project: WritingProject):
    quote = "the count came up\nshort  both times"  # whitespace drifted
    outcomes, _ = relocate_items(BODY, [_item("f_1", quote)], project, provider=FakeProvider([]))
    assert outcomes[0].outcome == "persisting"
    assert outcomes[0].tier == "normalized"
    span = outcomes[0].span
    assert BODY[span.start : span.end] == "the count came up short both times"


def test_relocate_llm_fallback_classifies_resolved_and_new_quote(project: WritingProject):
    new_quote = "Holt watched her from the gangway"
    provider = FakeProvider(
        [
            _json_resp(
                {
                    "items": [
                        {"id": "f_gone", "verdict": "RESOLVED", "quote": ""},
                        {"id": "f_moved", "verdict": "PERSISTS", "quote": new_quote},
                    ]
                }
            )
        ]
    )
    outcomes, usage = relocate_items(
        BODY,
        [_item("f_gone", "text that was deleted entirely from this draft"),
         _item("f_moved", "another quote that no longer appears anywhere")],
        project,
        provider=provider,
    )
    assert len(provider.requests) == 1  # ONE batched call
    by_id = {o.item_id: o for o in outcomes}
    assert by_id["f_gone"].outcome == "resolved"
    assert by_id["f_moved"].outcome == "persisting"
    assert BODY[by_id["f_moved"].span.start : by_id["f_moved"].span.end] == new_quote
    assert usage.input_tokens > 0


def test_relocate_hallucinated_quote_is_unlocatable(project: WritingProject):
    provider = FakeProvider(
        [_json_resp({"items": [{"id": "f_1", "verdict": "PERSISTS", "quote": "never in the body"}]})]
    )
    outcomes, _ = relocate_items(
        BODY, [_item("f_1", "some quote that drifted away")], project, provider=provider
    )
    assert outcomes[0].outcome == "unlocatable"


def test_relocate_provider_error_degrades_to_unlocatable(project: WritingProject):
    provider = FakeProvider([ProviderError("rate limited")])
    outcomes, _ = relocate_items(
        BODY, [_item("f_1", "some quote that drifted away")], project, provider=provider
    )
    assert outcomes[0].outcome == "unlocatable"  # never raises


def test_relocate_empty_items_makes_no_call(project: WritingProject):
    provider = FakeProvider([])
    outcomes, _ = relocate_items(BODY, [], project, provider=provider)
    assert outcomes == []
    assert provider.requests == []


def test_relocate_llm_disabled_degrades_drifters(project: WritingProject):
    provider = FakeProvider([])
    outcomes, _ = relocate_items(
        BODY, [_item("f_1", "gone quote")], project, provider=provider, llm_relocate=False
    )
    assert outcomes[0].outcome == "unlocatable"
    assert provider.requests == []


# ---------------------------------------------------------------------------
# U5: session engine
# ---------------------------------------------------------------------------


def test_session_end_to_end(project: WritingProject):
    proj = _small_roster_project(project)
    # 2 takes then 2 cross-exams
    provider = FakeProvider([_take_resp(), _take_resp(), _crossexam_resp(), _crossexam_resp()])
    res = run_room_session(proj, chapter=1, provider=provider)

    assert len(provider.requests) == 4  # R17: 2 editors x 1 pass + 2 cross-exams
    assert res.findings_count == 2
    sources = set()
    record = json.loads(Path(res.json_path).read_text(encoding="utf-8"))
    for f in record["findings"]:
        sources.add(f["source"])
        assert f["span"] is not None
    assert sources == {"room:dev-editor:pacing", "room:line-editor:line"}
    # notebooks gained items and the opinion refresh landed
    nb = Notebook(proj, "dev-editor", proj.config.room)
    assert len(nb.items()) == 1
    assert nb.opinion == "the running opinion"
    # ledger has room.session
    actions = [e.action for e in Ledger(proj.root).tail(50)]
    assert "room.session" in actions
    assert "room.notebook.update" in actions
    # record round-trips and the markdown exists
    assert record["id"] == res.session_id
    assert Path(res.md_path).exists()


def test_session_persisting_flag_escalates_and_is_on_the_record(project: WritingProject):
    proj = _small_roster_project(project)
    nb = Notebook(proj, "line-editor", proj.config.room)
    nb.upsert_item("f_old", 1, "the count came up short both times",
                   "flagged over-explaining here", "minor", "line", "ch-01-1")
    provider = FakeProvider([_take_resp(), _take_resp(), _crossexam_resp(), _crossexam_resp()])
    res = run_room_session(proj, chapter=1, provider=provider)

    assert len(provider.requests) == 4  # deterministic re-locate: no extra call
    item = Notebook(proj, "line-editor", proj.config.room).get("f_old")
    assert item["status"] == "persisting"
    assert item["escalations"] == 1
    md = Path(res.md_path).read_text(encoding="utf-8")
    assert "On the record" in md
    assert "flagged over-explaining here" in md
    assert res.relocated.get("persisting") == 1
    # escalation framing reaches the take prompts
    take_user = provider.requests[1].messages[0].content  # line-editor's take
    assert "persists" in take_user


def test_session_comment_answered_in_crossexam(project: WritingProject):
    from stoner.room.comments import CommentStore

    proj = _small_roster_project(project)
    store = CommentStore(proj)
    c = store.add(1, quote="the count came up short both times", text="is this too slow?")
    answer = _crossexam_resp(
        comment_responses=[{"comment_id": c.id, "response": "no, it earns its length"}]
    )
    provider = FakeProvider([_take_resp(), _take_resp(), answer, _crossexam_resp()])
    res = run_room_session(proj, chapter=1, provider=provider)

    assert len(provider.requests) == 4  # answered in cross-exam: no follow-up call
    assert res.obligations_unmet == []
    updated = store.get(1, c.id)
    assert updated.status == "answered"
    assert updated.responses[0].editor == "dev-editor"
    assert updated.responses[0].text == "no, it earns its length"
    # the comment is injected into cross-exam prompts
    assert c.id in provider.requests[2].messages[0].content


def test_session_unanswered_comment_triggers_exactly_one_followup(project: WritingProject):
    from stoner.room.comments import CommentStore

    proj = _small_roster_project(project)
    store = CommentStore(proj)
    c = store.add(1, quote="", text="what is Holt hiding?")
    followup = _json_resp(
        {"comment_responses": [{"comment_id": c.id, "response": "the room thinks: the ledger"}]}
    )
    provider = FakeProvider(
        [_take_resp(), _take_resp(), _crossexam_resp(), _crossexam_resp(), followup]
    )
    res = run_room_session(proj, chapter=1, provider=provider)

    assert len(provider.requests) == 5  # exactly ONE follow-up
    assert res.obligations_unmet == []
    updated = store.get(1, c.id)
    assert updated.status == "answered"
    assert updated.responses[0].editor == "room"


def test_session_silent_followup_flags_unmet_obligation(project: WritingProject):
    from stoner.room.comments import CommentStore

    proj = _small_roster_project(project)
    c = CommentStore(proj).add(1, quote="", text="unanswerable question")
    provider = FakeProvider(
        [
            _take_resp(),
            _take_resp(),
            _crossexam_resp(),
            _crossexam_resp(),
            _json_resp({"comment_responses": []}),  # follow-up stays silent
        ]
    )
    res = run_room_session(proj, chapter=1, provider=provider)
    assert res.obligations_unmet == [c.id]
    assert any("unmet" in n for n in res.notes)
    record = json.loads(Path(res.json_path).read_text(encoding="utf-8"))
    assert record["obligations_unmet"] == [c.id]


def test_session_take_failure_degrades_to_info_finding(project: WritingProject):
    proj = _small_roster_project(project)
    provider = FakeProvider(
        [ProviderError("boom"), _take_resp(), _crossexam_resp(), _crossexam_resp()]
    )
    res = run_room_session(proj, chapter=1, provider=provider)
    record = json.loads(Path(res.json_path).read_text(encoding="utf-8"))
    degraded = [f for f in record["findings"] if f["source"] == "room:dev-editor"]
    assert len(degraded) == 1
    assert degraded[0]["severity"] == "info"
    assert "take failed" in degraded[0]["issue"]
    assert any("take failed" in n for n in res.notes)
    # the other editor's take and both cross-exams still ran
    assert len(provider.requests) == 4


def test_session_disagreements_stored_verbatim(project: WritingProject):
    proj = _small_roster_project(project)
    xe = _crossexam_resp(
        disagreements=[{"finding_id": "f_x", "note": "this cut guts the scene"}],
        agreements=[{"finding_id": "f_y", "note": "seconded"}],
    )
    provider = FakeProvider([_take_resp(), _take_resp(), xe, _crossexam_resp()])
    res = run_room_session(proj, chapter=1, provider=provider)
    assert res.disagreements == 1
    assert res.agreements == 1
    record = json.loads(Path(res.json_path).read_text(encoding="utf-8"))
    assert record["cross_exam"][0]["disagreements"][0]["note"] == "this cut guts the scene"
    md = Path(res.md_path).read_text(encoding="utf-8")
    assert "disagrees" in md and "this cut guts the scene" in md


def test_session_default_roster_call_count_matches_r17(project: WritingProject):
    """Default 4-editor roster: 6 passes + 4 cross-exams = 10 calls, no more."""
    provider = FakeProvider(lambda i, req: _take_resp() if i < 6 else _crossexam_resp())
    res = run_room_session(project, chapter=1, provider=provider)
    assert len(provider.requests) == 10
    assert res.editors == [
        "developmental-editor",
        "line-editor",
        "continuity-pedant",
        "first-reader",
    ]


def test_session_book_scope_assembles_six_full_eight_summaries(project: WritingProject):
    """14 chapters: the last 6 go in verbatim, the first 8 as memory summaries."""
    proj = _small_roster_project(project)
    memory = Memory(proj)
    for n in range(1, 15):
        proj.write_chapter(n, {"title": f"Ch {n}"}, f"Chapter {n} body text. " + BODY)
        memory.set_chapter_summary(n, f"summary of chapter {n}")
    provider = FakeProvider([_take_resp(), _take_resp(), _crossexam_resp(), _crossexam_resp()])
    res = run_room_session(proj, book=True, provider=provider)

    assert res.scope == "book"
    take_user = provider.requests[0].messages[0].content
    full = [n for n in range(1, 15) if f"## Chapter {n} (full text)" in take_user]
    summarized = [n for n in range(1, 15) if f"## Chapter {n} (summary only)" in take_user]
    assert full == [9, 10, 11, 12, 13, 14]
    assert summarized == [1, 2, 3, 4, 5, 6, 7, 8]
    assert "summary of chapter 3" in take_user
    record = json.loads(Path(res.json_path).read_text(encoding="utf-8"))
    assert record["chapters_full"] == [9, 10, 11, 12, 13, 14]
    assert record["chapters_summarized"] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_session_works_with_text_only_provider(project: WritingProject):
    """R18: the whole session is plain completions -- supports_tools=False works."""
    proj = _small_roster_project(project)

    class TextOnlyProvider(FakeProvider):
        supports_tools = False

    provider = TextOnlyProvider(
        [_take_resp(), _take_resp(), _crossexam_resp(), _crossexam_resp()]
    )
    res = run_room_session(proj, chapter=1, provider=provider)
    assert res.findings_count == 2
    assert all(not req.tools for req in provider.requests)


def test_session_reconciles_human_dismissal_before_relocation(project: WritingProject):
    """A human dismissal in a saved session record silences the notebook item."""
    proj = _small_roster_project(project)
    nb = Notebook(proj, "line-editor", proj.config.room)
    nb.upsert_item("f_old", 1, "the count came up short both times", "old flag",
                   "minor", "line", "ch-01-1")
    record = {
        "id": "ch-01-1",
        "findings": [{"id": "f_old", "source": "room:line-editor:line", "status": "dismissed"}],
    }
    sessions_dir = proj.root / ".stoner" / "room" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "ch-01-1.json").write_text(json.dumps(record), encoding="utf-8")

    provider = FakeProvider([_take_resp(), _take_resp(), _crossexam_resp(), _crossexam_resp()])
    res = run_room_session(proj, chapter=1, provider=provider)
    item = Notebook(proj, "line-editor", proj.config.room).get("f_old")
    assert item["status"] == "dismissed"
    assert item["escalations"] == 0  # never re-located, never escalated
    assert res.relocated == {}


def test_session_requires_chapter_or_book(project: WritingProject):
    with pytest.raises(ValueError):
        run_room_session(project, provider=FakeProvider([]))


def test_session_unknown_pass_noted_and_skipped(project: WritingProject):
    cfg = yaml.safe_load((project.root / "stoner.yaml").read_text(encoding="utf-8"))
    cfg["room"] = {
        "editors": [{"name": "Solo", "persona": "p", "passes": ["line", "nope"]}]
    }
    (project.root / "stoner.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    proj = WritingProject(project.root)
    provider = FakeProvider([_take_resp(), _crossexam_resp()])
    res = run_room_session(proj, chapter=1, provider=provider)
    assert len(provider.requests) == 2  # only the known pass ran
    assert any("unknown pass" in n for n in res.notes)
