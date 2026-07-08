"""Cross-feature wiring tests (integration plan 011 U4).

Every hook exercised here is opt-in and default-off: the off-state assertions
prove v0.2.0 behavior is byte-identical, and the on-state assertions prove the
consume-if-present arm now that both sides of each seam exist. No network --
providers are scripted text-only fakes.

Covers:
- voice gate in run_write (plan 001 U5)
- cast auto-update hook after the archivist (plan 002 U7)
- `write --tournament N` (plan 003 seam)
- book-mode tournament slots (plan 003 seam)
- Writers' Room roster referencing the interiority pass (plan 005 seam)
- ship check blocking on unfired guns (plan 010 seam)
- readers bench Elo ratings (plan 008 seam)
- context_pack budget with facts + motifs sections (plan 011 U4)
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.archaeology import DraftStore
from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.cli.main import app
from stoner.ledger import Ledger
from stoner.pipelines.book import run_book
from stoner.pipelines.write import run_write
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.types import CompletionRequest, CompletionResponse, Usage
from stoner.voice.fingerprint import learn_fingerprint, save_fingerprint

# ---------------------------------------------------------------------------
# Deterministic contrasting prose (mirrors tests/test_voice.py generators so
# this file stays self-contained -- no cross-test-module imports).
# ---------------------------------------------------------------------------

_A_LATINATE = [
    "consideration", "circumstance", "melancholy", "providence", "contemplation",
    "magnitude", "desolation", "apprehension", "tranquility", "significance",
    "observation", "recollection", "immensity", "procession", "inclination",
]
_A_NOUNS = [
    "sea", "ship", "harbor", "street", "water", "sky", "shore", "sailor",
    "voyage", "tide", "wind", "mast", "wave", "fog", "lantern",
]
_A_VERBS = [
    "drifted", "lingered", "gathered", "beheld", "wandered", "regarded",
    "surveyed", "remembered", "followed", "carried",
]
_A_ADJS = ["grey", "silent", "vast", "weary", "ancient", "dim", "slow", "cold", "heavy", "pale"]

_B_NOUNS = ["road", "sun", "dog", "door", "truck", "rain", "boy", "gun", "hill", "creek", "barn", "fence"]
_B_VERBS = ["ran", "stopped", "looked", "took", "went", "hit", "left", "came", "stood"]
_B_ADJS = ["hot", "dry", "old", "flat", "dark"]


def long_breath_text(seed: int, paragraphs: int) -> str:
    """Voice A: long clause-chained sentences, latinate, no dialogue."""
    rng = random.Random(seed)
    out = []
    for _ in range(paragraphs):
        sents = []
        for _ in range(rng.randint(3, 5)):
            clauses = []
            for _ in range(rng.randint(2, 4)):
                clauses.append(
                    f"the {rng.choice(_A_ADJS)} {rng.choice(_A_NOUNS)} "
                    f"{rng.choice(_A_VERBS)} beneath the {rng.choice(_A_ADJS)} "
                    f"{rng.choice(_A_NOUNS)}, in the {rng.choice(_A_LATINATE)} "
                    f"of the {rng.choice(_A_NOUNS)}"
                )
            s = rng.choice([", and ", "; and ", ", for ", "; "]).join(clauses)
            sents.append(s[0].upper() + s[1:] + ".")
        out.append(" ".join(sents))
    return "\n\n".join(out)


def clipped_text(seed: int, paragraphs: int) -> str:
    """Voice B: clipped declaratives, dialogue, contractions."""
    rng = random.Random(seed)
    out = []
    for _ in range(paragraphs):
        sents = []
        for _ in range(rng.randint(4, 7)):
            kind = rng.random()
            if kind < 0.3:
                sents.append(f'"It\'s {rng.choice(_B_ADJS)} out," he said.')
            elif kind < 0.6:
                sents.append(f"The {rng.choice(_B_NOUNS)} was {rng.choice(_B_ADJS)}.")
            else:
                sents.append(
                    f"He {rng.choice(_B_VERBS)} the {rng.choice(_B_NOUNS)}. She didn't wait."
                )
        out.append(" ".join(sents))
    return "\n\n".join(out)


A_TRAIN = long_breath_text(1, 60)
OFF_VOICE = clipped_text(99, 30)        # scores ~49 against an A fingerprint
IN_VOICE = long_breath_text(99, 40)     # scores ~0 against an A fingerprint

# A clean McCarthy-ish paragraph used for slop-clean drafts/takes.
_PARAGRAPH = (
    "He walked to the window and stood there a while. The yard was bare. "
    "A dog crossed the road and stopped, looked back, and went on. He "
    "thought of his father's hands, how they had held the plow, and of the "
    "dry fields in August. Nothing in the house moved. "
)
LONG_PROSE = _PARAGRAPH * 8

ARCHIVIST_JSON = json.dumps(
    {"summary": "A quiet chapter.", "facts": [], "new_entities": [], "thread_updates": []}
)
BOOK_REVIEW_JSON = json.dumps({"overall": "", "verdict": "ship", "findings": []})


def _resp(text: str) -> CompletionResponse:
    return CompletionResponse(text=text, stop_reason="end", usage=Usage(input_tokens=10, output_tokens=20))


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "testbook")
    scaffold_project(p, "testbook")
    return p


def _ledger_actions(project: WritingProject, prefix: str = "") -> list[str]:
    return [e.action for e in Ledger(project.root).tail(2000) if e.action.startswith(prefix)]


# ===========================================================================
# 1. Voice gate in run_write (plan 001 U5)
# ===========================================================================


class VoiceProvider(Provider):
    """Text-only: single-shot draft returns off-voice prose; revise returns
    in-voice prose wrapped in the BEGIN/END CHAPTER sentinels."""

    name = "voice-fake"
    supports_tools = False

    def __init__(self, draft_body: str, revised_body: str):
        self.draft_body = draft_body
        self.revised_body = revised_body
        self.requests: list[CompletionRequest] = []
        self.revise_calls = 0

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        user = req.messages[0].content
        if "Rewrite the ENTIRE chapter" in user or "Accepted findings" in user:
            self.revise_calls += 1
            return _resp(f"SUMMARY: back in voice\nBEGIN CHAPTER\n{self.revised_body}\nEND CHAPTER")
        return _resp(self.draft_body)


def _disable_slop_gate(project: WritingProject) -> None:
    """Neutralize the slop gate so a test isolates a later stage. The scripted
    fixture prose is deliberately off-voice/repetitive; without this the slop
    gate would fire first and mask the stage under test."""
    project.config.gates.slop_max_score = 1000.0
    project.config.gates.slop_block_severities = []


def _voice_project(project: WritingProject, gate: bool, learn: bool, max_drift: float = 25.0) -> None:
    _disable_slop_gate(project)
    project.config.voice.gate = gate
    project.config.voice.max_drift_score = max_drift
    if learn:
        save_fingerprint(project, learn_fingerprint([("a.md", A_TRAIN)]))


def test_voice_gate_off_default_is_byte_identical(project):
    # Default config: voice.gate is False -> no voice check, no voice.* ledger.
    _disable_slop_gate(project)  # keep the off-voice draft from tripping the slop gate
    provider = VoiceProvider(OFF_VOICE, IN_VOICE)
    res = run_write(project, 1, provider=provider, skip_archive=True)
    assert res.voice_before == -1.0 and res.voice_after == -1.0
    assert "voice.gate" not in _ledger_actions(project)
    assert provider.revise_calls == 0


def test_voice_gate_on_without_fingerprint_proceeds_with_note(project):
    _voice_project(project, gate=True, learn=False)
    provider = VoiceProvider(OFF_VOICE, IN_VOICE)
    res = run_write(project, 1, provider=provider, skip_archive=True)
    # No fingerprint -> the helper returns None; the write proceeds with a note
    # and never scores or revises.
    assert res.voice_before == -1.0 and res.voice_after == -1.0
    assert "voice.gate" not in _ledger_actions(project)
    assert any("no fingerprint" in n for n in res.notes)
    assert provider.revise_calls == 0


def test_voice_gate_on_off_voice_draft_triggers_bounded_revise(project):
    _voice_project(project, gate=True, learn=True, max_drift=25.0)
    provider = VoiceProvider(OFF_VOICE, IN_VOICE)
    res = run_write(project, 1, provider=provider, skip_archive=True)

    assert res.voice_before > 25.0           # off-voice draft failed the gate
    assert res.voice_after < 25.0            # in-voice revision now passes
    assert provider.revise_calls == 1
    assert res.revision_loops == 1
    actions = _ledger_actions(project)
    assert actions.count("voice.gate") == 1
    # The revision snapshot carries the voice-revise reason.
    reasons = [e.reason for e in DraftStore(project).load_manifest(1).entries]
    assert "voice-revise" in reasons


def test_voice_gate_respects_shared_revision_budget(project):
    # Revision keeps returning off-voice prose: the loop must stop at the
    # shared gates.max_revision_loops budget and leave a still-failing note.
    _voice_project(project, gate=True, learn=True, max_drift=25.0)
    project.config.gates.max_revision_loops = 2
    provider = VoiceProvider(OFF_VOICE, OFF_VOICE)
    res = run_write(project, 1, provider=provider, skip_archive=True)

    assert res.revision_loops == 2
    assert _ledger_actions(project).count("voice.gate") == 2
    assert any("voice gate still failing" in n for n in res.notes)


def test_voice_gate_on_in_voice_draft_no_extra_calls(project):
    _voice_project(project, gate=True, learn=True, max_drift=25.0)
    provider = VoiceProvider(IN_VOICE, IN_VOICE)
    res = run_write(project, 1, provider=provider, skip_archive=True)

    assert res.voice_before == res.voice_after
    assert res.voice_before < 25.0
    assert provider.revise_calls == 0
    assert "voice.gate" not in _ledger_actions(project)


# ===========================================================================
# 2. Cast auto-update hook after the archivist (plan 002 U7)
# ===========================================================================


class CastWriteProvider(Provider):
    """Text-only: draft -> archivist -> cast-curator, dispatched by content."""

    name = "cast-fake"
    supports_tools = False

    def __init__(self, cast_payload: str):
        self.cast_payload = cast_payload
        self.requests: list[CompletionRequest] = []
        self.cast_calls = 0
        self.archivist_calls = 0

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        user = req.messages[0].content
        if "cast curator" in user:
            self.cast_calls += 1
            return _resp(self.cast_payload)
        if "continuity archivist" in user:
            self.archivist_calls += 1
            return _resp(ARCHIVIST_JSON)
        return _resp(LONG_PROSE)


def _seed_cast_sheet(project: WritingProject, slug: str = "ruth", name: str = "Ruth") -> None:
    from stoner.interiority import CastSheet, CastStore

    CastStore(project).save(CastSheet(slug=slug, name=name))


def test_cast_hook_fires_with_sheets(project):
    _seed_cast_sheet(project)
    cast_json = json.dumps(
        {"ruth": {"new_knowledge": [{"fact": "the bank called", "how": "phone", "quote": "the phone rang"}]}}
    )
    provider = CastWriteProvider(cast_json)
    run_write(project, 1, provider=provider)
    assert provider.cast_calls == 1
    assert "cast.update" in _ledger_actions(project)


def test_cast_hook_no_sheets_zero_extra_calls(project):
    provider = CastWriteProvider("{}")
    run_write(project, 1, provider=provider)
    # No sheets -> the hook never calls the curator.
    assert provider.cast_calls == 0
    assert "cast.update" not in _ledger_actions(project)


def test_cast_hook_auto_update_false_zero_calls(project):
    _seed_cast_sheet(project)
    project.config.cast.auto_update = False
    provider = CastWriteProvider("{}")
    run_write(project, 1, provider=provider)
    assert provider.cast_calls == 0
    assert "cast.update" not in _ledger_actions(project)


def test_cast_hook_curator_failure_degrades_to_note(project):
    _seed_cast_sheet(project)
    provider = CastWriteProvider("this is not json at all")  # parse_cast_update raises
    res = run_write(project, 1, provider=provider)
    # The write still succeeded; the failure is a note, not an exception.
    assert res.words > 0
    assert any("cast auto-update failed" in n for n in res.notes)


# ===========================================================================
# 3 & 4. Tournament drafting (write --tournament N; book-mode slots)
# ===========================================================================


class TournamentWriteProvider(Provider):
    """Text-only provider covering every call a tournament-backed write/book run
    makes: per-take drafts (distinct, with a marker), blind judging, the
    archivist, and the whole-book review."""

    name = "tournament-fake"
    supports_tools = False

    def __init__(self, winner_marker: str = "TAKE2"):
        self.winner_marker = winner_marker
        self.requests: list[CompletionRequest] = []
        self.draft_calls = 0
        self.judge_calls = 0

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        user = req.messages[0].content
        if "## Verdict" in user and "## Take A" in user:
            self.judge_calls += 1
            take_a = user.split("## Take A", 1)[1].split("## Take B", 1)[0]
            winner = "A" if self.winner_marker in take_a else "B"
            loser = "B" if winner == "A" else "A"
            return _resp(json.dumps({"winner": winner, "steal": {"from": loser, "move": "x"}, "why": "y"}))
        if "continuity archivist" in user:
            return _resp(ARCHIVIST_JSON)
        if "Review the complete manuscript" in user:
            return _resp(BOOK_REVIEW_JSON)
        # A draft (take or single-shot): distinct, slop-clean, > 200 words.
        self.draft_calls += 1
        return _resp(f"{LONG_PROSE}\n\nTAKE{self.draft_calls}\n")


def test_write_tournament_applies_winner_then_runs_pipeline(project):
    project.config.tournament.graft = False  # keep the winner raw (no graft call)
    _disable_slop_gate(project)  # isolate the tournament seam from the slop gate
    provider = TournamentWriteProvider(winner_marker="TAKE2")

    res = run_write(project, 1, provider=provider, skip_archive=True, tournament=3)

    # Three takes drafted and judged (round-robin for n=3 -> 3 pairs x 2 orders).
    assert provider.draft_calls == 3
    assert provider.judge_calls == 6

    # The winning take landed in the manuscript.
    _, body = project.read_chapter(1)
    assert "TAKE2" in body
    assert res.words > 200

    # Snapshots carry both the per-take `draft` reason and `tournament-graft`.
    reasons = [e.reason for e in DraftStore(project).load_manifest(1).entries]
    assert "draft" in reasons
    assert "tournament-graft" in reasons

    # Ledger order: every tournament.* entry precedes every pipeline.write.* one.
    actions = _ledger_actions(project)
    tour_idx = [i for i, a in enumerate(actions) if a.startswith("tournament.")]
    write_idx = [i for i, a in enumerate(actions) if a.startswith("pipeline.write.")]
    assert tour_idx and write_idx
    assert max(tour_idx) < min(write_idx)


def _seed_beats(project: WritingProject, number: int) -> None:
    project.write(
        f"outline/beats/ch-{number:02d}.md",
        f"# Chapter {number} beats\n\n- something happens\n",
    )


def test_book_tournament_slot_only_opening(project):
    _seed_beats(project, 1)
    _seed_beats(project, 2)
    _disable_slop_gate(project)
    project.config.tournament.graft = False
    project.config.tournament.slot_takes = {"opening": 3}  # no `ending` entry
    provider = TournamentWriteProvider(winner_marker="TAKE2")

    run_book(project, provider=provider, review_every=0, max_review_rounds=1, tournament=True)

    starts = [
        e.target for e in Ledger(project.root).tail(2000) if e.action == "tournament.start"
    ]
    # ch-01 is the `opening` slot (in slot_takes) -> one tournament; ch-02 is the
    # `ending` slot but has no slot_takes entry -> a single draft.
    assert starts == ["manuscript/ch-01.md"]


def test_book_without_tournament_flag_is_byte_identical(project):
    _seed_beats(project, 1)
    _seed_beats(project, 2)
    _disable_slop_gate(project)
    project.config.tournament.slot_takes = {"opening": 3}
    provider = TournamentWriteProvider()

    run_book(project, provider=provider, review_every=0, max_review_rounds=1)

    assert "tournament.start" not in _ledger_actions(project)


# ===========================================================================
# 5. Writers' Room roster referencing the interiority pass (plan 005 seam)
# ===========================================================================


class RoomProvider(Provider):
    """Callable-free room fake: take calls return one finding; cross-exam calls
    return an empty position payload. Dispatched by prompt content."""

    name = "room-fake"

    def __init__(self):
        self.requests: list[CompletionRequest] = []
        self.take_prompts: list[str] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        user = req.messages[0].content
        if "Your findings this session" in user:  # cross-examination
            return _resp(json.dumps({
                "agreements": [], "disagreements": [], "priority_rank": [],
                "comment_responses": [], "notebook_note": "",
            }))
        # a pass take
        self.take_prompts.append(user)
        return _resp(json.dumps({
            "findings": [{
                "severity": "major", "category": "interiority",
                "quote": "he said nothing", "issue": "a broken refusal",
            }],
            "summary": "interiority take",
        }))


def _interiority_room(project: WritingProject) -> None:
    from stoner.config import EditorSpec

    project.config.room.editors = [
        EditorSpec(name="Interiority Editor", persona="You watch private state.", passes=["interiority"])
    ]


def test_room_interiority_pass_produces_findings_with_cast(project):
    from stoner.interiority import CastSheet, CastStore, KnowledgeEntry
    from stoner.room.session import run_room_session

    project.write_chapter(1, {"title": "One", "status": "draft"}, LONG_PROSE)
    CastStore(project).save(
        CastSheet(
            slug="ruth", name="Ruth",
            knowledge=[KnowledgeEntry(id="k001", fact="RUTH_SECRET_MARKER", learned_in=1)],
        )
    )
    _interiority_room(project)
    provider = RoomProvider()

    res = run_room_session(project, chapter=1, provider=provider)

    assert res.findings_by_editor.get("interiority-editor", 0) >= 1
    # The interiority pass saw the private sheet (dramatic-irony channel).
    assert any("RUTH_SECRET_MARKER" in p for p in provider.take_prompts)


def test_room_interiority_pass_degrades_without_cast(project):
    from stoner.room.session import run_room_session

    project.write_chapter(1, {"title": "One", "status": "draft"}, LONG_PROSE)
    _interiority_room(project)
    provider = RoomProvider()

    # No cast sheets: the pass still runs, the prompt says so, and the session
    # completes without raising.
    res = run_room_session(project, chapter=1, provider=provider)
    assert res.session_id
    assert any("No cast sheets exist" in p for p in provider.take_prompts)


def test_opt_in_passes_are_not_in_the_default_roster():
    from stoner.config import StonerConfig
    from stoner.room.roster import OPT_IN_PASSES, load_roster

    roster = load_roster(StonerConfig().room)
    assigned = {p for e in roster for p in e.passes}
    assert not (set(OPT_IN_PASSES) & assigned)
    assert OPT_IN_PASSES == ("interiority", "verisimilitude")


# ===========================================================================
# 6. Ship check blocks on unfired guns (plan 010 seam)
# ===========================================================================


def test_ship_check_cli_blocks_on_unfired_gun(project, monkeypatch):
    project.write_chapter(1, {"title": "One", "status": "final"}, LONG_PROSE)
    CanonStore(project).plant_promise("p1", "the gun on the wall", "threat", opened_in="ch-01")
    monkeypatch.chdir(project.root)

    result = CliRunner().invoke(app, ["ship", "check"])
    assert result.exit_code == 1
    assert "unfired" in result.output


def test_ship_check_cli_passes_with_only_open_thread_warning(project, monkeypatch):
    project.write_chapter(1, {"title": "One", "status": "final"}, LONG_PROSE)
    CanonStore(project).add_thread("t1", "a texture thread", opened_in="ch-01", status="open")
    monkeypatch.chdir(project.root)

    result = CliRunner().invoke(app, ["ship", "check"])
    assert result.exit_code == 0, result.output


# ===========================================================================
# 7. Readers bench Elo ratings (plan 008 seam)
# ===========================================================================


_BENCH_ROSTER = ["maya_riven", "owen_shelby", "marcus_hale", "lena_voss"]


class ScriptedReader(Provider):
    name = "reader-fake"

    def __init__(self, fn: Callable[[int, CompletionRequest], CompletionResponse]):
        self.fn = fn
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        return self.fn(idx, req)


def _all_pick_a(idx, req):
    body = req.messages[0].content
    ids = [pid for pid in _BENCH_ROSTER if f"id: {pid}" in body]
    readers = {pid: {"pick": "a", "loss_a": [], "loss_b": []} for pid in ids}
    return CompletionResponse(text=json.dumps({"readers": readers}), usage=Usage())


def test_bench_ratings_match_elo_math(project, tmp_path: Path):
    """The tournament rating utilities are present, so bench aggregates via Elo;
    the reported ratings must match a hand-run of the same Elo updates."""
    from stoner.readers.bench import run_bench
    from stoner.readers.comps import add_comp
    from stoner.tournament import rating

    project.write_chapter(1, {"title": "One"}, "The manuscript begins in a counting room.\n")
    project.write_chapter(2, {"title": "Two"}, "The manuscript turns on a forged letter.\n")
    comp_file = tmp_path / "comp_source.txt"
    comp_file.write_text(
        "Chapter 1\n\nThe comp opens on a quiet harbor at dawn.\n\n"
        "Chapter 2\n\nBy the second chapter the comp has found its feet.\n",
        encoding="utf-8",
    )
    add_comp(project, comp_file, title="Comp")

    result = run_bench(
        project, "comp", roster=_BENCH_ROSTER,
        provider=ScriptedReader(_all_pick_a), run_id="wire-bench",
    )

    assert result.aggregation == "elo"
    # Recompute the Elo exactly as bench does: one game per decisive pick.
    total_m = sum(row["manuscript"] for row in result.per_chapter)
    total_c = sum(row["comp"] for row in result.per_chapter)
    rm = rc = rating.INITIAL_RATING
    for _ in range(total_m):
        rm, rc = rating.update(rm, rc, rating.WIN)
    for _ in range(total_c):
        rm, rc = rating.update(rm, rc, rating.LOSS)
    assert result.ratings == {"manuscript": round(rm, 1), "comp": round(rc, 1)}


# ===========================================================================
# 8. context_pack budget with facts + motifs (plan 011 U4)
# ===========================================================================


def test_context_pack_drops_whole_sections_under_tight_budget(project):
    store = CanonStore(project)
    # Short premise + style (they anchor the top of the priority order and must
    # never drop); then long, lower-priority sections a tight budget must drop
    # whole rather than half-quote.
    store.project.write("canon/premise.md", "---\nkind: premise\n---\n\nThe keeper's last winter.")
    store.project.write("canon/style.md", "---\nkind: style\n---\n\nSpare, salt-worn sentences.")
    store.add_motif("mo1", "the river", anchors="river; water", meaning="the passage of time and memory that carries the story")
    store.add_motif("mo2", "the lantern", anchors="lantern", meaning="fragile hope held up against the encroaching dark")
    store.upsert_fact("tide-fact", {"name": "Tides", "claim": "Spring tides follow the new and full moon by a day or two.", "confidence": "high"})
    store.add_thread("t1", "an open thread about the brother who never came back from the north", opened_in="ch-01", status="open")

    tight = store.context_pack(max_chars=95)
    generous = store.context_pack(max_chars=12000)

    # Premise and style never drop; they fit whole even under the tight budget.
    assert "## Premise" in tight and "The keeper's last winter." in tight
    assert "## Style" in tight and "Spare, salt-worn sentences." in tight
    # Every lower-priority section drops whole under the tight budget...
    assert "## Motifs" not in tight
    assert "## Facts" not in tight
    assert "## Open Threads" not in tight
    # ...but all are present with a generous budget (proving it is the budget,
    # not absence, that drops them, and that facts/motifs coexist under it).
    assert "## Motifs" in generous
    assert "## Facts" in generous
    assert "## Open Threads" in generous
