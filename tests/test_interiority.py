"""Tests for cast interiority: sheets/store, privacy boundary, curator,
boundedness, and the interiority review pass.

No network: a scripted FakeProvider stands in for a vendor backend, mirroring
`tests/test_review.py`. The scene sim has its own file (`test_interiority_scene.py`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.interiority import (
    CastError,
    CastSheet,
    CastStore,
    CuratorError,
    KnowledgeEntry,
    Lie,
    Refusal,
    Wants,
    apply_cast_update,
    check_boundedness,
    knowledge_asof,
    parse_cast_update,
    private_digest,
    run_cast_check,
    run_cast_update,
)
from stoner.interiority.curator import cast_update_prompt
from stoner.ledger import Ledger
from stoner.pipelines.common import chapter_context
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.review.passes import PASSES, build_context
from stoner.types import CompletionRequest, CompletionResponse, Severity, Usage

SENTINEL = "ZZQARIAKNOWSXYZ"  # a string that must never reach writer context

CH1_BODY = (
    "Ruth turned the notice over in her hands.\n"
    '"We are fine," she told Dale, and did not look at him.\n'
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, responses: list | Callable[[int], CompletionResponse], supports_tools: bool = True):
        self.responses = responses
        self.supports_tools = supports_tools
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        item = self.responses(idx) if callable(self.responses) else self.responses[min(idx, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _resp(text: str) -> CompletionResponse:
    return CompletionResponse(text=text, usage=Usage(input_tokens=10, output_tokens=5))


def _json_resp(payload: dict) -> CompletionResponse:
    return _resp("```json\n" + json.dumps(payload) + "\n```")


def _write_character(project: WritingProject, name: str, slug: str, wants_fears: str = "") -> None:
    body = f"## Voice\n\nClipped, wary.\n\n## Wants / Fears\n\n{wants_fears}\n\n## Arc\n\n...\n"
    project.write(f"canon/characters/{slug}.md", f"---\nname: {name}\nrole: protagonist\n---\n\n{body}")


@pytest.fixture
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Test Book")
    scaffold_project(proj, "Test Book")
    proj.write_chapter(1, {"title": "The Notice", "pov": "Ruth"}, CH1_BODY)
    _write_character(proj, "Ruth Vann", "ruth-vann", "Stated: keep the house. Real: be forgiven.")
    _write_character(proj, "Dale Kestner", "dale-kestner")
    return proj


def _seed_sheet(store: CastStore, slug: str, name: str, **kw) -> CastSheet:
    sheet = CastSheet(slug=slug, name=name, **kw)
    store.save(sheet)
    return sheet


# ---------------------------------------------------------------------------
# U1: sheet + store round-trip and id assignment
# ---------------------------------------------------------------------------


def test_sheet_roundtrip_and_id_sequence(project: WritingProject):
    store = CastStore(project)
    sheet = CastSheet(
        slug="ruth-vann",
        name="Ruth Vann",
        wants=Wants(stated="keep the house", real="be forgiven"),
        knowledge=[
            KnowledgeEntry(id="k001", fact="the notice arrived", learned_in=1),
            KnowledgeEntry(id="k002", fact="Dale forged the deed", learned_in=0, secret=True),
        ],
        lies=[Lie(id="l001", claim="we are fine", truth="k001")],
    )
    store.save(sheet)
    loaded = store.load("ruth-vann")
    assert loaded.wants.real == "be forgiven"
    assert [e.id for e in loaded.knowledge] == ["k001", "k002"]
    assert CastStore.next_knowledge_id(loaded) == "k003"
    assert CastStore.next_lie_id(loaded) == "l002"


def test_init_from_canon_seeds_and_refuses_second(project: WritingProject):
    store = CastStore(project)
    sheet = store.init_from_canon("Ruth Vann")
    assert sheet.slug == "ruth-vann"
    assert sheet.canon_ref == "canon/characters/ruth-vann.md"
    assert "keep the house" in sheet.seed_notes
    assert store.exists("ruth-vann")
    with pytest.raises(CastError, match="already exists"):
        store.init_from_canon("Ruth Vann")


def test_init_from_canon_unknown_name(project: WritingProject):
    with pytest.raises(CastError, match="no canon character"):
        CastStore(project).init_from_canon("Nobody At All")


def test_knowledge_asof_filters_by_chapter():
    sheet = CastSheet(
        slug="x",
        name="X",
        knowledge=[
            KnowledgeEntry(id="k001", fact="backstory fact", learned_in=0),
            KnowledgeEntry(id="k002", fact="early fact", learned_in=3),
            KnowledgeEntry(id="k003", fact="late fact", learned_in=9),
        ],
    )
    got = {e.id for e in knowledge_asof(sheet, 4)}
    assert got == {"k001", "k002"}


def test_private_digest_respects_max_chars_and_isolation():
    sheet = CastSheet(
        slug="ruth-vann",
        name="Ruth Vann",
        knowledge=[KnowledgeEntry(id=f"k{i:03d}", fact=f"fact number {i} " * 5, learned_in=1) for i in range(1, 20)],
    )
    other = CastSheet(slug="dale-kestner", name="Dale Kestner", knowledge=[KnowledgeEntry(id="k001", fact="DALE_ONLY_SECRET", learned_in=1)])
    digest = private_digest(sheet, chapter=5, max_chars=300)
    assert len(digest) <= 300
    assert "DALE_ONLY_SECRET" not in digest
    assert other.name  # other exists but never leaks in


def test_private_digest_drops_oldest_nonsecret_keeps_secret():
    sheet = CastSheet(
        slug="x",
        name="X",
        knowledge=[
            KnowledgeEntry(id="k001", fact="old public fact " * 10, learned_in=1),
            KnowledgeEntry(id="k002", fact="THE_SECRET", learned_in=1, secret=True),
        ],
    )
    digest = private_digest(sheet, chapter=5, max_chars=120)
    assert "THE_SECRET" in digest  # secret survives the trim


def test_corrupt_sheet_json_names_the_file(project: WritingProject):
    store = CastStore(project)
    (project.root / ".stoner" / "cast").mkdir(parents=True, exist_ok=True)
    project.write(".stoner/cast/ruth-vann.json", "{not valid json")
    with pytest.raises(CastError, match="ruth-vann.json"):
        store.load("ruth-vann")


# ---------------------------------------------------------------------------
# U1: the privacy boundary (the heart of the feature)
# ---------------------------------------------------------------------------


def test_privacy_boundary_cast_state_never_in_writer_context(project: WritingProject):
    store = CastStore(project)
    _seed_sheet(
        store,
        "ruth-vann",
        "Ruth Vann",
        wants=Wants(stated="keep the house", real=SENTINEL),
        fears=[SENTINEL],
        knowledge=[KnowledgeEntry(id="k001", fact=SENTINEL, learned_in=0, secret=True)],
        lies=[Lie(id="l001", claim=SENTINEL, truth="k001")],
        refusals=[Refusal(topic=SENTINEL, reason=SENTINEL)],
    )
    # 1. canon context pack
    assert SENTINEL not in CanonStore(project).context_pack()
    # 2. chapter_context values (what the writer prompt is built from)
    for value in chapter_context(project, 1).values():
        assert SENTINEL not in value
    # 3. review build_context fields
    ctx = build_context(project, 1)
    for value in (ctx.chapter_body, ctx.canon_digest, ctx.memory_context, ctx.style_excerpt, ctx.prior_tail, ctx.voice_digest):
        assert SENTINEL not in value


# ---------------------------------------------------------------------------
# U2: curator diff/apply
# ---------------------------------------------------------------------------


def test_curator_prompt_carries_digest_and_chapter():
    prompt = cast_update_prompt("chapter text here", "SHEETS_DIGEST_MARKER")
    assert "SHEETS_DIGEST_MARKER" in prompt
    assert "chapter text here" in prompt
    assert "STRICT JSON" in prompt


def test_curator_dry_run_then_auto(project: WritingProject):
    store = CastStore(project)
    _seed_sheet(store, "ruth-vann", "Ruth Vann")
    parsed = {"ruth-vann": {"new_knowledge": [{"fact": "the bank called", "how": "told", "quote": "the phone rang"}]}}

    dry = apply_cast_update(store, parsed, chapter=3, auto=False)
    assert dry.dry_run is True
    assert len(dry.applied) == 1
    assert store.load("ruth-vann").knowledge == []  # nothing written

    applied = apply_cast_update(store, parsed, chapter=3, auto=True)
    assert applied.dry_run is False
    entries = store.load("ruth-vann").knowledge
    assert len(entries) == 1
    assert entries[0].id == "k001"
    assert entries[0].learned_in == 3


def test_curator_want_shift_conflict_when_wants_set(project: WritingProject):
    store = CastStore(project)
    _seed_sheet(store, "ruth-vann", "Ruth Vann", wants=Wants(stated="a", real="b"))
    parsed = {"ruth-vann": {"want_shift": {"stated": "different", "real": "changed"}}}
    res = apply_cast_update(store, parsed, chapter=2, auto=True)
    assert len(res.conflicts) == 1
    assert res.conflicts[0].kind == "want_shift"
    # sheet unchanged despite auto=True
    assert store.load("ruth-vann").wants.stated == "a"


def test_curator_lie_exposure_conflict_and_idempotent(project: WritingProject):
    store = CastStore(project)
    _seed_sheet(store, "ruth-vann", "Ruth Vann", lies=[Lie(id="l001", claim="all fine", exposed_in=4, active=False)])
    # already exposed in a DIFFERENT chapter -> conflict
    diff_ch = apply_cast_update(store, {"ruth-vann": {"lie_updates": [{"id": "l001", "exposed": True}]}}, chapter=6, auto=True)
    assert any(c.kind == "lie_exposure" for c in diff_ch.conflicts)
    # SAME chapter -> idempotent no-op (redraft re-run)
    same_ch = apply_cast_update(store, {"ruth-vann": {"lie_updates": [{"id": "l001", "exposed": True}]}}, chapter=4, auto=True)
    assert not same_ch.conflicts
    assert any("already exposed this chapter" in s["reason"] for s in same_ch.skipped)


def test_curator_duplicate_knowledge_idempotent_same_chapter(project: WritingProject):
    store = CastStore(project)
    _seed_sheet(store, "ruth-vann", "Ruth Vann", knowledge=[KnowledgeEntry(id="k001", fact="the bank called", learned_in=3)])
    res = apply_cast_update(store, {"ruth-vann": {"new_knowledge": [{"fact": "The Bank Called"}]}}, chapter=3, auto=True)
    assert not res.conflicts
    assert len(store.load("ruth-vann").knowledge) == 1  # no duplicate


def test_curator_unknown_lie_id_skipped(project: WritingProject):
    store = CastStore(project)
    _seed_sheet(store, "ruth-vann", "Ruth Vann")
    res = apply_cast_update(store, {"ruth-vann": {"lie_updates": [{"id": "l999", "exposed": True}]}}, chapter=2, auto=True)
    assert any("unknown lie id" in s["reason"] for s in res.skipped)


def test_curator_garbage_output_raises():
    with pytest.raises(CuratorError):
        parse_cast_update("no json here whatsoever")


def test_run_cast_update_ledgers(project: WritingProject):
    store = CastStore(project)
    _seed_sheet(store, "ruth-vann", "Ruth Vann")
    provider = FakeProvider([_json_resp({"ruth-vann": {"new_knowledge": [{"fact": "the bank called"}]}})])
    res = run_cast_update(project, 1, provider=provider, auto=True)
    assert len(res.applied) == 1
    lines = [e for e in Ledger(project.root).tail(20) if e.action == "cast.update"]
    assert lines and lines[-1].detail["applied"] == 1


def test_run_cast_update_no_sheets_no_model_call(project: WritingProject):
    provider = FakeProvider([_json_resp({})])
    res = run_cast_update(project, 1, provider=provider, auto=True)
    assert res.applied == []
    assert provider.requests == []  # no model call when no sheets


# ---------------------------------------------------------------------------
# U3: boundedness (the pure check)
# ---------------------------------------------------------------------------


def _sheet_with(entries: list[KnowledgeEntry], slug="ruth-vann", name="Ruth Vann") -> CastSheet:
    return CastSheet(slug=slug, name=name, knowledge=entries)


def test_check_boundedness_flags_anachronistic_only():
    sheet = _sheet_with(
        [
            KnowledgeEntry(id="k001", fact="the deed", learned_in=9),
            KnowledgeEntry(id="k002", fact="backstory", learned_in=0),
        ]
    )
    attrs = [{"character": "ruth-vann", "entry_id": "k001", "quote": "the deed", "basis": "speaks"}]
    # reviewed at chapter 4: learned_in 9 > 4 -> violation
    at4 = check_boundedness(attrs, [sheet], chapter=4)
    assert len(at4) == 1 and at4[0].severity == Severity.major
    assert at4[0].category == "anachronistic-knowledge"
    # reviewed at 9 and 10: no violation
    assert check_boundedness(attrs, [sheet], chapter=9) == []
    assert check_boundedness(attrs, [sheet], chapter=10) == []
    # backstory never flags
    back = [{"character": "ruth-vann", "entry_id": "k002", "quote": "backstory", "basis": "thinks"}]
    assert check_boundedness(back, [sheet], chapter=1) == []


def test_check_boundedness_secret_escalates_to_critical():
    sheet = _sheet_with([KnowledgeEntry(id="k001", fact="the forgery", learned_in=9, secret=True)])
    attrs = [{"character": "ruth-vann", "entry_id": "k001", "quote": "the forgery", "basis": "acts"}]
    findings = check_boundedness(attrs, [sheet], chapter=4)
    assert findings[0].severity == Severity.critical


def test_check_boundedness_null_entry_is_info():
    sheet = _sheet_with([])
    attrs = [{"character": "ruth-vann", "entry_id": None, "quote": "some fact", "basis": "speaks"}]
    findings = check_boundedness(attrs, [sheet], chapter=4)
    assert findings[0].severity == Severity.info
    assert "cast update" in findings[0].suggestion


def test_check_boundedness_unknown_slug_no_crash():
    sheet = _sheet_with([])
    attrs = [{"character": "ghost", "entry_id": "k001", "quote": "x", "basis": "speaks"}]
    findings = check_boundedness(attrs, [sheet], chapter=4)
    assert findings[0].category == "unknown-character"


def test_run_cast_check_writes_report_and_ledgers(project: WritingProject):
    store = CastStore(project)
    _seed_sheet(store, "ruth-vann", "Ruth Vann", knowledge=[KnowledgeEntry(id="k001", fact="the deed", learned_in=9)])
    provider = FakeProvider([_json_resp({"attributions": [{"character": "ruth-vann", "entry_id": "k001", "quote": "the notice", "basis": "acts"}]})])
    report = run_cast_check(project, 1, provider=provider)
    assert report.kind == "cast"
    assert any(f.category == "anachronistic-knowledge" for f in report.findings)
    reviews = list((project.root / ".stoner" / "reviews").glob("cast-ch-01-*.json"))
    assert reviews
    saved = json.loads(reviews[0].read_text())
    assert saved["kind"] == "cast"
    assert (project.root / ".stoner" / "reviews").glob("cast-ch-01-*.md")
    assert [e for e in Ledger(project.root).tail(20) if e.action == "cast.check"]


def test_run_cast_check_empty_cast_no_model_call(project: WritingProject):
    provider = FakeProvider([_json_resp({"attributions": []})])
    report = run_cast_check(project, 1, provider=provider)
    assert report.findings == []
    assert "no cast sheets" in report.summary
    assert provider.requests == []


# ---------------------------------------------------------------------------
# U6: the interiority review pass
# ---------------------------------------------------------------------------


def test_interiority_pass_registered_and_embeds_sheets(project: WritingProject):
    assert "interiority" in PASSES
    store = CastStore(project)
    _seed_sheet(store, "ruth-vann", "Ruth Vann", refusals=[Refusal(topic="the fire", reason="guilt")])
    ctx = build_context(project, 1)
    system, user = PASSES["interiority"].build_prompt(ctx)
    assert "the fire" in user  # sheet content reaches the pass prompt
    assert "STRICT JSON" in user


def test_interiority_pass_empty_cast_instruction(project: WritingProject):
    ctx = build_context(project, 1)
    _system, user = PASSES["interiority"].build_prompt(ctx)
    assert "No cast sheets exist" in user
    assert PASSES["interiority"].parse('{"findings": [], "summary": "ok"}') == []


def test_interiority_pass_parses_findings(project: WritingProject):
    payload = {"findings": [
        {"severity": "major", "category": "lie", "quote": "we are fine", "issue": "dropped without exposure", "suggestion": "add a beat"},
        {"severity": "minor", "category": "want", "quote": "", "issue": "want collapse", "suggestion": "cut"},
    ]}
    findings = PASSES["interiority"].parse(json.dumps(payload))
    assert len(findings) == 2
    assert all(f.source == "review:interiority" for f in findings)


def test_run_review_interiority_end_to_end(project: WritingProject):
    from stoner.review.runner import run_review

    store = CastStore(project)
    _seed_sheet(store, "ruth-vann", "Ruth Vann")
    provider = FakeProvider([_json_resp({"findings": [{"severity": "info", "category": "irony", "quote": "", "issue": "missed setup", "suggestion": "use it"}], "summary": "s"})])
    report = run_review(project, 1, passes=["interiority"], provider=provider)
    assert report.passes == ["interiority"]
    assert any(f.source == "review:interiority" for f in report.findings)
