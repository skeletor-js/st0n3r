"""Foundation-pipeline tests. No network: providers are scripted fakes."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.cli.foundation_cmds import register as register_foundation_cmds
from stoner.cli.main import app
from stoner.pipelines.foundation import (
    FoundationError,
    run_brainstorm,
    run_canon_generation,
)
from stoner.project import WritingProject, split_frontmatter
from stoner.providers.base import Provider
from stoner.types import CompletionRequest, CompletionResponse, Usage

# `stoner brainstorm`/`stoner foundation` are wired onto the shared app by
# the orchestrator in production; for CLI tests we wire them ourselves.
register_foundation_cmds(app)

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


def text_response(text: str) -> CompletionResponse:
    return CompletionResponse(text=text, stop_reason="end", usage=Usage(input_tokens=10, output_tokens=20))


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "testbook")
    scaffold_project(p, "testbook")
    return p


# ---------------------------------------------------------------------------
# fixture JSON payloads
# ---------------------------------------------------------------------------

BRAINSTORM_JSON = """{
  "premise": {
    "logline": "A washed-up detective must solve one last case before the flooded city drowns for good.",
    "genre": "noir fantasy",
    "themes": ["guilt", "memory"],
    "promise": "A satisfying mystery with a bittersweet, earned ending.",
    "comps": ["Chinatown", "The City & The City"]
  },
  "style": {
    "voice": "Terse, hardboiled, present-tense interiority kept to a minimum.",
    "pov": "close third",
    "tense": "past",
    "rhythm_notes": "Short punchy sentences; vary length for emphasis.",
    "banned_words": ["glimmer", "shimmer"],
    "banned_phrases": ["heart pounding"]
  },
  "title_options": ["The Sunken City", "Drowned Ledger", "Last Tide"]
}"""

CHARACTERS_JSON = """{
  "characters": [
    {
      "name": "Aria Voss",
      "role": "protagonist",
      "age": "34",
      "appearance": {"hair": "black", "eyes": "green", "build": "lean", "distinguishing": "scar on jaw"},
      "relationships": {"mara-quill": "old partner, now rival"},
      "voice": "clipped, dry, distrustful of adjectives",
      "wants": "close one last case clean",
      "fears": "that she was wrong about the last one",
      "arc": "learns to trust someone besides herself"
    },
    {
      "name": "Mara Quill",
      "role": "antagonist",
      "age": "45",
      "appearance": {},
      "relationships": {},
      "voice": "smooth, procedural, never raises her voice",
      "wants": "keep the Concord's secret buried",
      "fears": "irrelevance once the city floods",
      "arc": "unravels as the flood exposes what she buried"
    }
  ]
}"""

CHARACTERS_REGEN_JSON = """{
  "characters": [
    {
      "name": "Mara Quill",
      "role": "antagonist",
      "age": "45",
      "appearance": {"hair": "silver", "eyes": "grey", "build": "spare", "distinguishing": "burn scar"},
      "relationships": {"aria-voss": "old partner, now rival"},
      "voice": "smooth, procedural, never raises her voice",
      "wants": "keep the Concord's secret buried",
      "fears": "irrelevance once the city floods",
      "arc": "unravels as the flood exposes what she buried"
    },
    {
      "name": "Corin Wren",
      "role": "supporting",
      "age": "29",
      "appearance": {},
      "relationships": {},
      "voice": "nervous, over-explains",
      "wants": "protection",
      "fears": "the Concord",
      "arc": "finds a spine"
    }
  ]
}"""

WORLD_JSON = """{
  "world": [
    {"name": "The Hollow", "type": "place", "rules": "no magic after dark", "description": "a sunken district", "history": "built on a buried river"},
    {"name": "The Concord", "type": "faction", "rules": "obeys the charter absolutely", "description": "the ruling council", "history": "formed after the last war"},
    {"name": "The Drowned Archive", "type": "item", "rules": "opens only to a blood name", "description": "a sealed vault", "history": "hidden before the flood"}
  ]
}"""

THREADS_JSON = """{
  "threads": [
    {"name": "who killed the duke", "opened_in": "ch-01", "notes": "central mystery"},
    {"name": "Aria's missing sister", "opened_in": "ch-01", "notes": "backstory thread"},
    {"name": "the Concord's hidden vote", "opened_in": "ch-02", "notes": "political thread"},
    {"name": "the sunken archive", "opened_in": "ch-01", "notes": "macguffin"},
    {"name": "Mara's true allegiance", "opened_in": "ch-03", "notes": "twist setup"}
  ]
}"""

OUTLINE_JSON = """{
  "acts": {
    "act1": "Aria arrives at the Hollow and finds the body.",
    "act2": "Aria investigates the Concord and is stonewalled.",
    "act3": "Aria opens the archive and confronts Mara."
  },
  "chapters": [
    {"number": 1, "title": "Arrival", "pov": "Aria Voss", "summary": "Aria arrives at the Hollow and finds the body.",
     "beats": {"goal": "identify the body", "conflict": "the Concord blocks her", "turn": "she finds a clue", "exit_state": "she is now a suspect"}},
    {"number": 2, "title": "The Vote", "pov": "Aria Voss", "summary": "Aria investigates the Concord's hidden vote.",
     "beats": {"goal": "get access to the vote records", "conflict": "Mara stonewalls her", "turn": "a witness talks", "exit_state": "Aria has a lead on the archive"}},
    {"number": 3, "title": "The Archive", "pov": "Aria Voss", "summary": "Aria opens the drowned archive.",
     "beats": {"goal": "open the archive", "conflict": "the wards fight her", "turn": "the archive reveals a name", "exit_state": "Aria knows who to confront"}}
  ]
}"""

EVALUATE_SHIP_JSON = (
    '{"weakest_element": "world", "why": "world is thin but workable", '
    '"fix_instructions": "", "verdict": "ship"}'
)

EVALUATE_ITERATE_JSON = (
    '{"weakest_element": "characters", "why": "the antagonist is flat and has no concrete secret", '
    '"fix_instructions": "give Mara a concrete, specific secret tied to the archive", "verdict": "iterate"}'
)


def _full_generation_responses() -> list[CompletionResponse]:
    return [
        text_response(CHARACTERS_JSON),
        text_response(WORLD_JSON),
        text_response(THREADS_JSON),
        text_response(OUTLINE_JSON),
        text_response(EVALUATE_SHIP_JSON),
    ]


def _brainstorm(project: WritingProject) -> None:
    provider = ScriptedProvider([text_response(BRAINSTORM_JSON)])
    run_brainstorm(project, "a detective in a flooding city", provider=provider)


# ---------------------------------------------------------------------------
# brainstorm
# ---------------------------------------------------------------------------


def test_brainstorm_writes_premise_and_style(project):
    provider = ScriptedProvider([text_response(BRAINSTORM_JSON)])
    res = run_brainstorm(project, "a detective in a flooding city", provider=provider)

    assert res.written == ["canon/premise.md", "canon/style.md"]
    assert res.title_options == ["The Sunken City", "Drowned Ledger", "Last Tide"]

    premise_text = project.read("canon/premise.md")
    assert "A washed-up detective must solve one last case" in premise_text
    assert "- Genre: noir fantasy" in premise_text
    assert "Chinatown, The City & The City" in premise_text
    assert "- guilt" in premise_text and "- memory" in premise_text
    assert "A satisfying mystery" in premise_text
    # instructive template comments are preserved
    assert "PROTAGONIST wants GOAL" in premise_text

    style_text = project.read("canon/style.md")
    assert "Terse, hardboiled" in style_text
    assert "- Point of view: close third" in style_text
    assert "- Tense: past" in style_text
    assert "Short punchy sentences" in style_text


def test_brainstorm_banned_block_parses_via_canon_store(project):
    provider = ScriptedProvider([text_response(BRAINSTORM_JSON)])
    run_brainstorm(project, "a detective in a flooding city", provider=provider)

    store = CanonStore(project)
    words, phrases = store.banned_terms()
    assert "glimmer" in words
    assert "shimmer" in words
    # template defaults are preserved alongside the model's additions
    assert "delve" in words
    assert "heart pounding" in phrases
    assert any("couldn't help but" in p for p in phrases)


def test_brainstorm_refuses_overwrite_without_force(project):
    _brainstorm(project)
    provider2 = ScriptedProvider([text_response(BRAINSTORM_JSON)])
    with pytest.raises(FoundationError, match="already have content"):
        run_brainstorm(project, "a second seed", provider=provider2)


def test_brainstorm_force_overwrites(project):
    _brainstorm(project)
    other_json = BRAINSTORM_JSON.replace("The Sunken City", "A Different Title")
    provider2 = ScriptedProvider([text_response(other_json)])
    res = run_brainstorm(project, "a second seed", provider=provider2, force=True)
    assert "A Different Title" in res.title_options
    assert "A Different Title" not in project.read("canon/premise.md")  # title isn't written to premise.md


def test_brainstorm_empty_seed_raises(project):
    with pytest.raises(FoundationError):
        run_brainstorm(project, "   ", provider=ScriptedProvider([text_response(BRAINSTORM_JSON)]))


# ---------------------------------------------------------------------------
# canon generation
# ---------------------------------------------------------------------------


def test_canon_generation_requires_filled_premise(project):
    provider = ScriptedProvider(_full_generation_responses())
    with pytest.raises(FoundationError, match="brainstorm"):
        run_canon_generation(project, provider=provider)


def test_canon_generation_end_to_end(project):
    _brainstorm(project)
    provider = ScriptedProvider(_full_generation_responses())
    res = run_canon_generation(project, provider=provider, characters=2, world_entries=3)

    store = CanonStore(project)

    # -- characters -------------------------------------------------------
    assert res.characters == ["aria-voss", "mara-quill"]
    char_entries = store.list_entries(kind="character")
    assert len(char_entries) == 2
    aria = store.get_character("aria-voss")
    assert aria is not None
    assert aria.frontmatter["role"] == "protagonist"
    assert aria.frontmatter["age"] == "34"
    assert aria.frontmatter["appearance"]["eyes"] == "green"
    assert aria.frontmatter["relationships"] == {"mara-quill": "old partner, now rival"}
    assert "clipped, dry" in aria.body
    assert "close one last case clean" in aria.body

    # -- world --------------------------------------------------------------
    assert res.world == ["the-hollow", "the-concord", "the-drowned-archive"]
    world_entries = store.list_entries(kind="world")
    assert len(world_entries) == 3
    hollow = store.get_world("the-hollow")
    assert hollow is not None
    assert hollow.frontmatter["type"] == "place"
    assert "no magic after dark" in hollow.body

    # -- threads --------------------------------------------------------------
    assert res.threads == ["t1", "t2", "t3", "t4", "t5"]
    rows = store.threads()
    assert len(rows) == 5
    assert rows[0].thread == "who killed the duke"
    assert all(r.status == "open" for r in rows)

    # -- outline --------------------------------------------------------------
    assert res.chapters == [1, 2, 3]
    outline_text = project.read("outline/outline.md")
    assert "Aria arrives at the Hollow" in outline_text
    assert "| 1 | Aria Voss |" in outline_text
    for n in (1, 2, 3):
        beats_text = project.read(f"outline/beats/ch-{n:02d}.md")
        fm, body = split_frontmatter(beats_text)
        assert fm["chapter"] == n
        assert fm["pov"] == "Aria Voss"
        assert body.strip()  # goal/conflict/turn/exit_state filled
    from stoner.canon.store import extract_section

    ch1 = project.read("outline/beats/ch-01.md")
    assert "identify the body" in extract_section(ch1, "Goal")
    assert "she is now a suspect" in extract_section(ch1, "Exit State")

    # -- evaluate --------------------------------------------------------------
    assert res.loops == 0
    assert res.verdict == "ship"
    assert res.usage.input_tokens > 0


def test_evaluate_loop_regenerates_weakest_once(project):
    _brainstorm(project)
    responses = [
        text_response(CHARACTERS_JSON),
        text_response(WORLD_JSON),
        text_response(THREADS_JSON),
        text_response(OUTLINE_JSON),
        text_response(EVALUATE_ITERATE_JSON),
        text_response(CHARACTERS_REGEN_JSON),
        text_response(EVALUATE_SHIP_JSON),
    ]
    provider = ScriptedProvider(responses)
    res = run_canon_generation(project, provider=provider, characters=2, world_entries=3)

    assert res.loops == 1
    assert res.verdict == "ship"
    # characters were regenerated: new roster replaces the old one
    assert res.characters == ["mara-quill", "corin-wren"]
    store = CanonStore(project)
    assert store.get_character("aria-voss") is None or "aria-voss" not in res.characters
    assert store.get_character("corin-wren") is not None
    assert len(provider.requests) == 7
    # the regeneration call carried the evaluator's fix instructions
    regen_request = provider.requests[5]
    assert "concrete, specific secret" in regen_request.system


def test_idempotence_skips_existing_elements(project):
    _brainstorm(project)
    provider1 = ScriptedProvider(_full_generation_responses())
    first = run_canon_generation(project, provider=provider1, characters=2, world_entries=3)

    provider2 = ScriptedProvider([text_response(EVALUATE_SHIP_JSON)])
    second = run_canon_generation(project, provider=provider2, characters=2, world_entries=3)

    assert len(provider2.requests) == 1  # only the evaluate call ran
    assert sorted(second.characters) == sorted(first.characters)
    assert sorted(second.world) == sorted(first.world)
    assert sorted(second.threads) == sorted(first.threads)
    assert second.chapters == first.chapters
    assert any("characters" in n and "skipped" in n for n in second.notes)
    assert any("world" in n and "skipped" in n for n in second.notes)
    assert any("threads" in n and "skipped" in n for n in second.notes)
    assert any("outline" in n and "skipped" in n for n in second.notes)


def test_force_regenerates_existing_elements(project):
    _brainstorm(project)
    provider1 = ScriptedProvider(_full_generation_responses())
    run_canon_generation(project, provider=provider1, characters=2, world_entries=3)

    provider2 = ScriptedProvider(
        [
            text_response(CHARACTERS_REGEN_JSON),
            text_response(WORLD_JSON),
            text_response(THREADS_JSON),
            text_response(OUTLINE_JSON),
            text_response(EVALUATE_SHIP_JSON),
        ]
    )
    res = run_canon_generation(project, provider=provider2, characters=2, world_entries=3, force=True)
    assert res.characters == ["mara-quill", "corin-wren"]
    assert not res.notes  # nothing skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    result = runner.invoke(app, ["init", "mybook", "--path", str(tmp_path / "mybook")])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path / "mybook")
    return tmp_path / "mybook"


def test_cli_brainstorm(project_dir: Path, monkeypatch):
    import stoner.pipelines.common as common_mod

    provider = ScriptedProvider([text_response(BRAINSTORM_JSON)])
    monkeypatch.setattr(common_mod, "get_provider", lambda model, config: (provider, "fake-model"))

    result = runner.invoke(app, ["brainstorm", "a detective in a flooding city"])
    assert result.exit_code == 0, result.output
    assert "Brainstormed" in result.output
    assert "washed-up detective" in result.output
    assert "The Sunken City" in result.output
    assert (project_dir / "canon" / "premise.md").exists()
    assert "noir fantasy" in (project_dir / "canon" / "premise.md").read_text()


def test_cli_brainstorm_refuses_without_force(project_dir: Path, monkeypatch):
    import stoner.pipelines.common as common_mod

    provider = ScriptedProvider([text_response(BRAINSTORM_JSON)])
    monkeypatch.setattr(common_mod, "get_provider", lambda model, config: (provider, "fake-model"))
    result = runner.invoke(app, ["brainstorm", "seed one"])
    assert result.exit_code == 0, result.output

    provider2 = ScriptedProvider([text_response(BRAINSTORM_JSON)])
    monkeypatch.setattr(common_mod, "get_provider", lambda model, config: (provider2, "fake-model"))
    result2 = runner.invoke(app, ["brainstorm", "seed two"])
    assert result2.exit_code == 1
    assert "already have content" in result2.output


def test_cli_foundation_end_to_end(project_dir: Path, monkeypatch):
    import stoner.pipelines.common as common_mod

    provider = ScriptedProvider([text_response(BRAINSTORM_JSON)])
    monkeypatch.setattr(common_mod, "get_provider", lambda model, config: (provider, "fake-model"))
    result = runner.invoke(app, ["brainstorm", "a detective in a flooding city"])
    assert result.exit_code == 0, result.output

    gen_provider = ScriptedProvider(_full_generation_responses())
    monkeypatch.setattr(common_mod, "get_provider", lambda model, config: (gen_provider, "fake-model"))
    result2 = runner.invoke(app, ["foundation", "--characters", "2", "--world", "3"])
    assert result2.exit_code == 0, result2.output
    assert "characters" in result2.output
    assert "evaluate:" in result2.output
    assert (project_dir / "canon" / "characters" / "aria-voss.md").exists()
    assert (project_dir / "outline" / "beats" / "ch-03.md").exists()
