"""Regression tests for the confirmed findings from the QA review pass."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from stoner.canon.archivist import apply_updates, diff_against_canon
from stoner.canon.memory import Memory
from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.cli.main import app
from stoner.engine.tools import default_registry, write_chapter
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.types import CompletionRequest, CompletionResponse, Finding, Usage

runner = CliRunner()


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "qabook")
    scaffold_project(p, "qabook")
    return p


# --- archivist: nested frontmatter fields ---------------------------------


def _add_character(project: WritingProject) -> CanonStore:
    store = CanonStore(project)
    project.write(
        "canon/characters/mara.md",
        "---\nname: Mara\nage: 30\nappearance:\n  eyes: gray\n  hair: black\n---\n\n## Voice\n",
    )
    return store


def test_archivist_diffs_nested_appearance_fields(project):
    store = _add_character(project)
    facts = [{"entity": "Mara", "kind": "character", "field": "eyes", "value": "blue", "quote": "q"}]
    conflicts = diff_against_canon(facts, store)
    assert len(conflicts) == 1
    assert conflicts[0].canon_value == "gray"

    # dotted path form too
    facts = [{"entity": "Mara", "kind": "character", "field": "appearance.eyes", "value": "blue"}]
    assert len(diff_against_canon(facts, store)) == 1

    # same value -> no conflict
    facts = [{"entity": "Mara", "kind": "character", "field": "eyes", "value": "Gray"}]
    assert diff_against_canon(facts, store) == []


def test_archivist_nested_apply_preserves_siblings(project):
    store = _add_character(project)
    memory = Memory(project)
    parsed = {
        "summary": "s",
        "facts": [{"entity": "Mara", "kind": "character", "field": "scar", "value": "left cheek"}],
        "new_entities": [],
        "thread_updates": [],
    }
    # 'scar' is new: applied nested? It's not in appearance, goes top-level.
    apply_updates(store, memory, parsed, 1, auto=True)
    entry = store.get_character("mara")
    assert entry.frontmatter["scar"] == "left cheek"
    # nested update must not clobber appearance siblings
    parsed["facts"] = [{"entity": "Mara", "kind": "character", "field": "appearance.build", "value": "wiry"}]
    apply_updates(store, memory, parsed, 1, auto=True)
    entry = store.get_character("mara")
    assert entry.frontmatter["appearance"]["build"] == "wiry"
    assert entry.frontmatter["appearance"]["eyes"] == "gray"
    assert entry.frontmatter["appearance"]["hair"] == "black"


def test_archivist_timeline_idempotent(project):
    store = _add_character(project)
    memory = Memory(project)
    parsed = {
        "summary": "s",
        "facts": [{"entity": "Mara", "kind": "timeline", "field": "event", "value": "The barn burns"}],
        "new_entities": [],
        "thread_updates": [],
    }
    apply_updates(store, memory, parsed, 3, auto=True)
    apply_updates(store, memory, parsed, 3, auto=True)
    rows = [r for r in store.timeline_rows() if r.event == "The barn burns"]
    assert len(rows) == 1


# --- revise: refuses to destroy a chapter ----------------------------------


class OneShotProvider(Provider):
    name = "oneshot"
    supports_tools = True

    def __init__(self, text: str):
        self.text = text

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(text=self.text, usage=Usage(input_tokens=1, output_tokens=1))


def test_revise_refuses_empty_body(project):
    from stoner.review.revise import revise_chapter

    body = ("A sentence of ordinary prose that carries its own weight. " * 40).strip()
    project.write_chapter(1, {"title": "One"}, body)
    provider = OneShotProvider("SUMMARY: gutted\nBEGIN CHAPTER\n\nEND CHAPTER")
    findings = [Finding(source="review:line", issue="cut everything")]
    with pytest.raises(ValueError, match="refusing to overwrite"):
        revise_chapter(project, 1, findings, model="x/y", provider=provider)
    _, after = project.read_chapter(1)
    assert after == body  # untouched


# --- write_chapter tool merges frontmatter ---------------------------------


def test_write_chapter_tool_preserves_pov_and_status(project):
    project.write_chapter(2, {"title": "Two", "pov": "Mara", "status": "revised"}, "Old body.")
    out = write_chapter(project, 2, body="New body with different words entirely.")
    assert not out.startswith("ERROR")
    fm, body = project.read_chapter(2)
    assert fm["pov"] == "Mara"
    assert fm["status"] == "revised"
    assert fm["title"] == "Two"
    assert "New body" in body


def test_slop_check_tool_registered(project):
    reg = default_registry()
    assert "slop_check" in reg.names()
    project.write_chapter(3, {"title": "T"}, "She delved into the tapestry of it all. " * 30)
    from stoner.engine.tools import slop_check

    out = slop_check(project, 3)
    assert "slop score" in out
    assert "tapestry" in out


# --- agent loop-guard nudge reaches the model -------------------------------


def test_loop_guard_nudge_is_a_user_message(project):
    from stoner.engine.agent import Agent
    from stoner.ledger import Ledger
    from stoner.types import ToolCall

    class RepeatingProvider(Provider):
        name = "repeat"
        supports_tools = True

        def __init__(self):
            self.requests: list[CompletionRequest] = []

        def complete(self, req: CompletionRequest) -> CompletionResponse:
            self.requests.append(req)
            return CompletionResponse(
                tool_calls=[ToolCall(id=f"c{len(self.requests)}", name="word_count", arguments={})],
                stop_reason="tool_use",
                usage=Usage(input_tokens=1, output_tokens=1),
            )

    provider = RepeatingProvider()
    agent = Agent(provider, "m", default_registry(), project, Ledger(project.root), "t")
    agent.run(task="loop", system="s", max_turns=10)
    nudged = [
        m
        for req in provider.requests
        for m in req.messages
        if m.role == "user" and "identical arguments" in m.content
    ]
    assert nudged, "nudge should be delivered as a user-role message the mappers keep"


# --- chapters >= 1000 stay visible ------------------------------------------


def test_four_digit_chapters_listed(project):
    project.write_chapter(1000, {"title": "Millennium"}, "Words in a far future chapter.")
    numbers = [c.number for c in project.chapters()]
    assert 1000 in numbers


# --- CLI: keyless creation flows --------------------------------------------


def test_cli_chapter_new_and_import_and_beats(project, monkeypatch, tmp_path):
    monkeypatch.chdir(project.root)
    r = runner.invoke(app, ["chapter", "new", "4", "--title", "The Gate", "--pov", "Mara"])
    assert r.exit_code == 0, r.output
    fm, _ = project.read_chapter(4)
    assert fm["title"] == "The Gate"
    # refuses overwrite
    r = runner.invoke(app, ["chapter", "new", "4"])
    assert r.exit_code == 1

    src = tmp_path / "old.md"
    src.write_text("My existing prose, carried in from the old draft.", encoding="utf-8")
    r = runner.invoke(app, ["chapter", "import", "5", str(src), "--title", "Carried"])
    assert r.exit_code == 0, r.output
    fm, body = project.read_chapter(5)
    assert "existing prose" in body and fm["status"] == "draft"

    r = runner.invoke(app, ["beats", "6"])
    assert r.exit_code == 0, r.output
    assert (project.root / "outline/beats/ch-06.md").exists()
    assert "chapter: 6" in (project.root / "outline/beats/ch-06.md").read_text()


def test_cli_canon_new(project, monkeypatch):
    monkeypatch.chdir(project.root)
    r = runner.invoke(app, ["canon", "new", "character", "Old Tom"])
    assert r.exit_code == 0, r.output
    assert (project.root / "canon/characters/old-tom.md").exists()
    r = runner.invoke(app, ["canon", "new", "character", "Old Tom"])
    assert r.exit_code == 1  # refuses overwrite
    r = runner.invoke(app, ["canon", "new", "world", "The Salt Court"])
    assert r.exit_code == 0
    assert (project.root / "canon/world/the-salt-court.md").exists()
    r = runner.invoke(app, ["canon", "new", "spell", "Nope"])
    assert r.exit_code == 1


# --- review runner keeps usage on parse failure ------------------------------


def test_runner_counts_usage_when_pass_unparseable(project):
    from stoner.review.runner import run_review

    class GarbageProvider(Provider):
        name = "garbage"
        supports_tools = True

        def complete(self, req: CompletionRequest) -> CompletionResponse:
            return CompletionResponse(text="not json at all", usage=Usage(input_tokens=100, output_tokens=7))

    project.write_chapter(1, {"title": "One"}, "Prose. " * 60)
    report = run_review(project, 1, passes=["line"], model="x/y", provider=GarbageProvider())
    assert report.usage.input_tokens == 100
    assert any("pass failed" in f.issue for f in report.findings)


# --- banned terms from style.md reach the detector ---------------------------


def test_banned_terms_flow_into_slop(project):
    style = project.read("canon/style.md")
    style = style.replace(
        "```yaml",
        "```yaml\nwords:\n  - frobnicate\nphrases:\n  - whispering pines\n", 1,
    )
    # write a style.md whose Banned block bans our probe terms
    project.write(
        "canon/style.md",
        "# Style\n\n## Banned\n\n```yaml\nwords:\n  - frobnicate\nphrases:\n  - whispering pines\n```\n",
    )
    project.write_chapter(7, {"title": "Probe"}, "He frobnicate the gate under the whispering pines. " * 10)
    from stoner.canon.store import CanonStore
    from stoner.slop import run_slop

    bw, bp = CanonStore(project).banned_terms()
    assert "frobnicate" in bw and "whispering pines" in bp
    _, body = project.read_chapter(7)
    report = run_slop(body, banned_words=bw, banned_phrases=bp)
    quotes = {f.quote.lower() for f in report.findings}
    assert "frobnicate" in quotes
    assert "whispering pines" in quotes
