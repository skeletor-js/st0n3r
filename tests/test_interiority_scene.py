"""Tests for the scene-simulation loop: multi-call collision, single-call
degradation, protocol parsing, and mode selection.

No network: a routing FakeProvider distinguishes character turns from the
assembly call by the system prompt and returns scripted output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.interiority import (
    CastSheet,
    CastStore,
    KnowledgeEntry,
    SceneError,
    parse_scene_block,
    run_scene,
)
from stoner.ledger import Ledger
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.types import CompletionRequest, CompletionResponse, Usage

RUTH_FACT = "RUTH_ONLY_FACT"
DALE_FACT = "DALE_ONLY_FACT"


# ---------------------------------------------------------------------------
# fixtures / provider
# ---------------------------------------------------------------------------


def _write_character(project: WritingProject, name: str, slug: str) -> None:
    project.write(
        f"canon/characters/{slug}.md",
        f"---\nname: {name}\nrole: supporting\n---\n\n## Voice\n\nClipped.\n\n## Wants / Fears\n\n...\n",
    )


@pytest.fixture
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Test Book")
    scaffold_project(proj, "Test Book")
    _write_character(proj, "Ruth Vann", "ruth-vann")
    _write_character(proj, "Dale Kestner", "dale-kestner")
    store = CastStore(proj)
    store.save(CastSheet(slug="ruth-vann", name="Ruth Vann", knowledge=[KnowledgeEntry(id="k001", fact=RUTH_FACT, learned_in=1)]))
    store.save(CastSheet(slug="dale-kestner", name="Dale Kestner", knowledge=[KnowledgeEntry(id="k001", fact=DALE_FACT, learned_in=1)]))
    return proj


def _u() -> Usage:
    return Usage(input_tokens=10, output_tokens=5)


def _turn_json(speech="", action="", private_note="", passed=False) -> str:
    return "```json\n" + json.dumps({"speech": speech, "action": action, "private_note": private_note, "pass": passed}) + "\n```"


def _is_assembly(req: CompletionRequest) -> bool:
    return "dialogue editor" in req.system.lower()


def _char_of(req: CompletionRequest) -> str:
    return "ruth" if "You are Ruth Vann" in req.system else "dale"


class SceneProvider(Provider):
    """Routes character turns to `turn_fn(req, self)`; assembly returns a script."""

    name = "scene-fake"

    def __init__(self, turn_fn, supports_tools: bool = True, script: str = "ASSEMBLED SCRIPT"):
        self.turn_fn = turn_fn
        self.supports_tools = supports_tools
        self.script = script
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        if _is_assembly(req):
            return CompletionResponse(text=self.script, usage=_u())
        return self.turn_fn(req, self)

    def turn_requests(self) -> list[CompletionRequest]:
        return [r for r in self.requests if not _is_assembly(r)]


# ---------------------------------------------------------------------------
# U4: multi-call collision
# ---------------------------------------------------------------------------


def test_multi_scene_alternates_and_isolates_private(project: WritingProject):
    def tf(req, prov):
        char = _char_of(req)
        count = sum(1 for r in prov.turn_requests() if _char_of(r) == char)
        if count == 1:  # first turn: speak with a private note
            if char == "ruth":
                return CompletionResponse(text=_turn_json(speech="Ruth speaks", private_note="RUTH_PRIVATE_XYZ"), usage=_u())
            return CompletionResponse(text=_turn_json(speech="Dale replies", private_note="DALE_PRIVATE_XYZ"), usage=_u())
        return CompletionResponse(text=_turn_json(passed=True), usage=_u())

    provider = SceneProvider(tf)
    res = run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="They talk.", provider=provider)

    # script + transcript
    assert res.script == "ASSEMBLED SCRIPT"
    assert res.stopped_reason == "all_passed"
    transcript = json.loads(Path(res.transcript_path).read_text())
    # private notes are saved to the transcript...
    saved_notes = [t.get("private_note") for t in transcript["turns"]]
    assert "RUTH_PRIVATE_XYZ" in saved_notes and "DALE_PRIVATE_XYZ" in saved_notes
    # ...but never appear in ANY request the provider saw (public transcript only)
    for r in provider.requests:
        blob = r.system + " " + " ".join(m.content for m in r.messages)
        assert "RUTH_PRIVATE_XYZ" not in blob
        assert "DALE_PRIVATE_XYZ" not in blob


def test_multi_scene_sheet_isolation(project: WritingProject):
    provider = SceneProvider(lambda req, prov: CompletionResponse(text=_turn_json(speech="hi", passed=True), usage=_u()))
    run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="b", provider=provider)
    for r in provider.turn_requests():
        if _char_of(r) == "ruth":
            assert RUTH_FACT in r.system and DALE_FACT not in r.system
        else:
            assert DALE_FACT in r.system and RUTH_FACT not in r.system


def test_multi_scene_all_pass_ends_early(project: WritingProject):
    provider = SceneProvider(lambda req, prov: CompletionResponse(text=_turn_json(passed=True), usage=_u()))
    res = run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="b", provider=provider)
    assert res.stopped_reason == "all_passed"
    assert len(res.turns) == 2  # one round, both pass


def test_multi_scene_token_budget_stop(project: WritingProject):
    project.config.cast.scene_token_budget = 20  # each turn spends 15
    provider = SceneProvider(lambda req, prov: CompletionResponse(text=_turn_json(speech="x"), usage=_u()))
    res = run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="b", provider=provider)
    assert res.stopped_reason == "token_budget"
    json.loads(Path(res.transcript_path).read_text())  # valid JSON


def test_multi_scene_malformed_turn_retries_then_raw(project: WritingProject):
    def tf(req, prov):
        char = _char_of(req)
        turns = [r for r in prov.turn_requests() if _char_of(r) == char]
        # ruth's first two calls (turn + retry) are garbage -> raw fallback
        if char == "ruth" and len(turns) <= 2:
            return CompletionResponse(text="this is not json at all", usage=_u())
        return CompletionResponse(text=_turn_json(passed=True), usage=_u())

    provider = SceneProvider(tf)
    res = run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="b", provider=provider)
    # sim completed, and ruth's garbage became raw-text speech
    assert any(t.speaker == "ruth-vann" and "not json" in t.speech for t in res.turns)


def test_multi_scene_ledgers_start_turn_done(project: WritingProject):
    provider = SceneProvider(lambda req, prov: CompletionResponse(text=_turn_json(passed=True), usage=_u()))
    run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="b", provider=provider)
    actions = [e.action for e in Ledger(project.root).tail(30)]
    assert "cast.scene.start" in actions
    assert "cast.scene.turn" in actions
    assert "cast.scene.done" in actions


def test_scene_unknown_slug_errors_before_model_call(project: WritingProject):
    provider = SceneProvider(lambda req, prov: CompletionResponse(text=_turn_json(), usage=_u()))
    with pytest.raises(SceneError, match="ghost"):
        run_scene(project, ["ruth-vann", "ghost"], chapter=1, brief="b", provider=provider)
    assert provider.requests == []


# ---------------------------------------------------------------------------
# U5: single-call degradation + protocol parser
# ---------------------------------------------------------------------------

_SCENE_BLOCK = (
    "Here is the scene:\n\n"
    "```scene\n"
    "RUTH-VANN> We are fine.\n"
    "DALE-KESTNER> You keep saying that.\n"
    "RUTH-VANN (private)> He knows.\n"
    "DALE-KESTNER [action] sets down the cup\n"
    "garbled line with no speaker marker\n"
    "```\n\nThat's the scene."
)


def test_parse_scene_block_shapes_turns_and_excludes_private():
    turns, notes = parse_scene_block(_SCENE_BLOCK, ["ruth-vann", "dale-kestner"])
    speeches = [t.speech for t in turns if t.speech]
    assert "We are fine." in speeches and "You keep saying that." in speeches
    privates = [t.private_note for t in turns if t.private_note]
    assert privates == ["He knows."]
    actions = [t.action for t in turns if t.action]
    assert actions == ["sets down the cup"]
    # the private line is not part of the public speech lines
    assert "He knows." not in speeches
    # the garbled line becomes a parse note, the rest survives
    assert any("garbled line" in n for n in notes)


def test_parse_scene_block_missing_fence_errors():
    with pytest.raises(SceneError, match="no ```scene"):
        parse_scene_block("just prose, no fence at all", ["ruth-vann"])


def test_parse_scene_block_unknown_slug_is_note():
    turns, notes = parse_scene_block("```scene\nGHOST> boo\n```", ["ruth-vann"])
    assert turns == []
    assert any("unknown speaker" in n for n in notes)


def _single_provider(script="ASSEMBLED") -> SceneProvider:
    def tf(req, prov):
        return CompletionResponse(text=_SCENE_BLOCK, usage=_u())

    return SceneProvider(tf, supports_tools=False, script=script)


def test_mode_auto_text_only_is_single_call(project: WritingProject):
    provider = _single_provider()
    res = run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="b", provider=provider)
    assert res.mode == "single"
    assert len(provider.turn_requests()) == 1  # exactly one scene completion


def test_mode_auto_tools_capable_is_multi(project: WritingProject):
    provider = SceneProvider(lambda req, prov: CompletionResponse(text=_turn_json(passed=True), usage=_u()), supports_tools=True)
    res = run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="b", provider=provider)
    assert res.mode == "multi"
    assert len(provider.turn_requests()) > 1


def test_explicit_single_forces_single_on_tools_provider(project: WritingProject):
    provider = SceneProvider(lambda req, prov: CompletionResponse(text=_SCENE_BLOCK, usage=_u()), supports_tools=True)
    res = run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="b", mode="single", provider=provider)
    assert res.mode == "single"
    assert len(provider.turn_requests()) == 1


def test_mode_parity_same_result_shape(project: WritingProject):
    multi = SceneProvider(lambda req, prov: CompletionResponse(text=_turn_json(speech="hi", passed=True), usage=_u()))
    single = _single_provider()
    r_multi = run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="b", mode="multi", provider=multi)
    r_single = run_scene(project, ["ruth-vann", "dale-kestner"], chapter=1, brief="b", mode="single", provider=single)
    # same SceneResult field shape
    assert set(vars(r_multi)) == set(vars(r_single))
    # same transcript schema keys
    keys_multi = set(json.loads(Path(r_multi.transcript_path).read_text()))
    keys_single = set(json.loads(Path(r_single.transcript_path).read_text()))
    assert keys_multi == keys_single
