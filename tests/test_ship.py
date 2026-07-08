"""Ship: manifest, readiness gate (U1), assembly + prose parser (U2), and
model-drafted blurbs (U6). No network -- providers are scripted fakes and the
corpus is read-only (copied to tmp_path when a test needs mutation)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from stoner.canon.memory import Memory
from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.cli.main import app
from stoner.config import StonerConfig
from stoner.ledger import Ledger
from stoner.project import WritingProject
from stoner.providers.base import Provider, ProviderError
from stoner.ship.manifest import ShipError, build_manifest, require_ready
from stoner.types import CompletionRequest, CompletionResponse, Usage

runner = CliRunner()


class ScriptedProvider(Provider):
    """Returns queued responses in order; repeats the last when exhausted."""

    name = "scripted"
    supports_tools = False

    def __init__(self, responses: list[CompletionResponse]):
        self.responses = list(responses)
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class BoomProvider(Provider):
    name = "boom"
    supports_tools = False

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        raise ProviderError("provider exploded")


def _text(t: str) -> CompletionResponse:
    return CompletionResponse(text=t, stop_reason="end", usage=Usage(input_tokens=5, output_tokens=7))

CORPUS = Path(__file__).resolve().parent.parent / "examples" / "novella"


def _make_project(tmp_path: Path, name: str = "book") -> WritingProject:
    p = WritingProject.create(tmp_path / name, name)
    scaffold_project(p, name)
    return p


def _chapter(project: WritingProject, number: int, status: str, title: str = "", body: str = "Text.") -> None:
    project.write_chapter(number, {"title": title, "status": status}, body)


def corpus_copy(tmp_path: Path) -> WritingProject:
    """A mutable copy of the read-only novella corpus."""
    dest = tmp_path / "novella"
    shutil.copytree(CORPUS, dest)
    return WritingProject(dest)


# ---------------------------------------------------------------------------
# U1: readiness / manifest
# ---------------------------------------------------------------------------


def test_status_blocker_names_chapter(tmp_path: Path):
    project = _make_project(tmp_path)
    _chapter(project, 1, "revised")
    _chapter(project, 2, "draft")
    manifest = build_manifest(project)
    assert not manifest.ready
    assert any("ch-02" in b and "draft" in b for b in manifest.blockers)


def test_chapter_gap_is_blocker(tmp_path: Path):
    project = _make_project(tmp_path)
    _chapter(project, 1, "revised")
    _chapter(project, 3, "revised")
    manifest = build_manifest(project)
    assert any("ch-02" in b and "gap" in b for b in manifest.blockers)


def test_all_final_and_resolved_threads_pass(tmp_path: Path):
    project = _make_project(tmp_path)
    _chapter(project, 1, "final")
    _chapter(project, 2, "final")
    store = CanonStore(project)
    store.add_thread("t1", "a thread", opened_in="ch-01", status="resolved")
    store.add_thread("t2", "abandoned one", opened_in="ch-01", status="abandoned")
    manifest = build_manifest(project)
    assert manifest.ready, manifest.blockers
    assert not manifest.warnings  # abandoned/resolved do not warn


def test_open_kindless_thread_is_warning_not_blocker(tmp_path: Path):
    project = _make_project(tmp_path)
    _chapter(project, 1, "revised")
    store = CanonStore(project)
    store.add_thread("t1", "texture thread", opened_in="ch-01", status="open")
    manifest = build_manifest(project)
    assert manifest.ready
    assert any("t1" in w for w in manifest.warnings)
    assert not manifest.blockers


def test_open_promise_row_blocks_when_promises_available(tmp_path: Path):
    project = _make_project(tmp_path)
    _chapter(project, 1, "revised")
    store = CanonStore(project)
    store.plant_promise("p1", "the gun on the wall", "threat", opened_in="ch-01")
    manifest = build_manifest(project)
    assert not manifest.ready
    assert any("p1" in b and "unfired gun" in b for b in manifest.blockers)


def test_open_promise_degrades_to_warning_when_method_absent(tmp_path: Path, monkeypatch):
    project = _make_project(tmp_path)
    _chapter(project, 1, "revised")
    store = CanonStore(project)
    store.plant_promise("p1", "the gun on the wall", "threat", opened_in="ch-01")
    # Simulate feature 7 absent: the promises() method does not exist.
    monkeypatch.delattr(CanonStore, "promises", raising=True)
    manifest = build_manifest(project)
    assert manifest.ready  # no promise blocker without the method
    assert any("p1" in w for w in manifest.warnings)


def test_require_ready_records_override_in_ledger(tmp_path: Path):
    from stoner.ledger import Ledger

    project = _make_project(tmp_path)
    _chapter(project, 1, "draft")  # a blocker
    manifest = require_ready(project, allow_incomplete=True)
    assert manifest.blockers  # blockers exist but override passes
    tail = Ledger(project.root).tail(1)[0]
    assert tail.action == "ship.check"
    assert tail.detail["allow_incomplete"] is True


def test_require_ready_refuses_on_blockers(tmp_path: Path):
    project = _make_project(tmp_path)
    _chapter(project, 1, "draft")
    with pytest.raises(ShipError, match="not ready to ship"):
        require_ready(project, allow_incomplete=False)


def test_ship_config_round_trips(tmp_path: Path):
    project = _make_project(tmp_path)
    reloaded = StonerConfig.load(project.root)
    dumped = reloaded.dump_yaml()
    assert "ship:" in dumped
    again = StonerConfig.model_validate(yaml.safe_load(dumped))
    assert again.ship.trim == "5.5x8.5"
    assert again.ship.audio.backend == "say"


def test_init_creates_export_dir(tmp_path: Path):
    result = runner.invoke(app, ["init", "freshbook", "--path", str(tmp_path / "fresh")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "fresh" / "export").is_dir()


def test_ship_check_cli_exit_code_on_blocker(tmp_path: Path, monkeypatch):
    project = _make_project(tmp_path)
    _chapter(project, 1, "revised")
    _chapter(project, 2, "draft")
    monkeypatch.chdir(project.root)
    result = runner.invoke(app, ["ship", "check"])
    assert result.exit_code == 1
    assert "BLOCKER" in result.output


def test_ship_check_corpus_passes_with_warnings(tmp_path: Path, monkeypatch):
    project = corpus_copy(tmp_path)
    monkeypatch.chdir(project.root)
    result = runner.invoke(app, ["ship", "check"])
    assert result.exit_code == 0, result.output
    # t1/t4/t5/t8 are open kind-less threads -> warnings.
    assert "warning" in result.output
    manifest = build_manifest(project)
    assert len(manifest.chapters) == 15
    assert manifest.ready


# ---------------------------------------------------------------------------
# U2: assembly + prose subset parser
# ---------------------------------------------------------------------------


def test_paragraph_preserves_curly_quotes_and_em_dashes():
    from stoner.ship.assemble import Paragraph, parse_prose

    body = "She said “sold” — plain and flat — and meant it."
    blocks = parse_prose(body)
    assert len(blocks) == 1
    assert isinstance(blocks[0], Paragraph)
    assert blocks[0].text == body


def test_scene_break_variants():
    from stoner.ship.assemble import Paragraph, SceneBreak, parse_prose

    for marker in ("* * *", "***", "---"):
        blocks = parse_prose(f"Before.\n\n{marker}\n\nAfter.")
        assert isinstance(blocks[0], Paragraph)
        assert isinstance(blocks[1], SceneBreak)
        assert isinstance(blocks[2], Paragraph)


def test_inline_em_and_strong_runs():
    from stoner.ship.assemble import parse_inline

    runs = parse_inline("she *leaned* in and hit it **hard** once")
    styled = [(r.text, r.style) for r in runs if r.style != "plain"]
    assert ("leaned", "em") in styled
    assert ("hard", "strong") in styled


def test_unbalanced_asterisk_degrades_to_literal():
    from stoner.ship.assemble import parse_inline

    # A single unmatched asterisk stays literal.
    single = parse_inline("a lone * star with no partner")
    assert "".join(r.text for r in single) == "a lone * star with no partner"
    assert all(r.style == "plain" for r in single)
    # An unclosed strong marker degrades rather than eating the line.
    unclosed = parse_inline("hit it **hard once")
    assert "".join(r.text for r in unclosed) == "hit it **hard once"
    assert all(r.style == "plain" for r in unclosed)


def test_chapter_title_fallback_and_dedication_omitted(tmp_path: Path):
    from stoner.ship.assemble import assemble_book

    project = _make_project(tmp_path)
    _chapter(project, 1, "revised", title="", body="Body.")
    manifest = build_manifest(project)
    book = assemble_book(project, manifest)
    assert book.chapters[0].title == "Chapter 1"
    assert not any(p.kind == "dedication" for p in book.front_matter)


def test_dedication_page_present_when_set(tmp_path: Path):
    from stoner.ship.assemble import assemble_book

    project = _make_project(tmp_path)
    project.config.ship.dedication = "For the growers."
    _chapter(project, 1, "revised")
    manifest = build_manifest(project)
    book = assemble_book(project, manifest)
    ded = [p for p in book.front_matter if p.kind == "dedication"]
    assert ded and ded[0].lines == ["For the growers."]


def test_corpus_assembly_is_lossless(tmp_path: Path):
    from stoner.project import count_words
    from stoner.ship.assemble import assemble_book

    project = corpus_copy(tmp_path)
    manifest = build_manifest(project)
    book = assemble_book(project, manifest)
    assert len(book.chapters) == 15
    assert book.chapters[0].title == "Cloning"
    # Word count of parsed paragraph text matches the raw chapter bodies.
    parsed_words = sum(count_words(ch.plain_text()) for ch in book.chapters)
    raw_words = 0
    for ref in manifest.chapters:
        _fm, raw_body = project.read_chapter(ref.number)
        raw_words += count_words(raw_body)
    assert parsed_words == raw_words


# ---------------------------------------------------------------------------
# U6: blurbs
# ---------------------------------------------------------------------------


def _blurb_project(tmp_path: Path) -> WritingProject:
    project = _make_project(tmp_path)
    project.config.ship.title = "Sungrown"
    project.write(
        "canon/premise.md",
        "# Sungrown\n\n## Logline\n\nA grower faces legalization.\n\n"
        "## Genre & Comps\n\nLiterary fiction. Comps: Stoner, Train Dreams.\n\n"
        "## Promise to the Reader\n\nAn earned, quiet ending.\n",
    )
    _chapter(project, 1, "revised", body="A distinctive body sentence about rockwool cubes.")
    _chapter(project, 2, "revised", body="Another body-only line nobody summarizes.")
    mem = Memory(project)
    mem.set_chapter_summary(1, "Ruth cuts clones and avoids the permit.")
    mem.set_chapter_summary(2, "Ruth waits in the county queue.")
    mem.rebuild_book_so_far()
    return project


def test_blurbs_creates_three_files_and_ledgers(tmp_path: Path):
    from stoner.ship.blurbs import run_blurbs

    project = _blurb_project(tmp_path)
    provider = ScriptedProvider([_text("SYN"), _text("QUERY"), _text("COVER")])
    res = run_blurbs(project, provider=provider)
    assert len(res.written) == 3
    assert project.read("export/synopsis.md") == "SYN"
    assert project.read("export/query-letter.md") == "QUERY"
    assert project.read("export/cover-brief.md") == "COVER"
    actions = [e.action for e in Ledger(project.root).tail(3)]
    assert actions == ["ship.synopsis", "ship.query", "ship.cover"]
    detail = Ledger(project.root).tail(1)[0].detail
    assert "output_tokens" in detail


def test_blurbs_skip_existing_unless_force(tmp_path: Path):
    from stoner.ship.blurbs import run_blurbs

    project = _blurb_project(tmp_path)
    project.write("export/synopsis.md", "HAND EDITED")
    res = run_blurbs(project, only="synopsis", provider=ScriptedProvider([_text("NEW")]))
    assert res.written == [] and "export/synopsis.md" in res.skipped
    assert project.read("export/synopsis.md") == "HAND EDITED"
    # --force overwrites.
    res2 = run_blurbs(project, only="synopsis", force=True, provider=ScriptedProvider([_text("NEW")]))
    assert res2.written == ["export/synopsis.md"]
    assert project.read("export/synopsis.md") == "NEW"


def test_blurb_context_has_summaries_and_comps_but_no_prose(tmp_path: Path):
    from stoner.ship.blurbs import build_blurb_context

    project = _blurb_project(tmp_path)
    ctx = build_blurb_context(project)
    blob = "\n".join(ctx.values())
    # Memory summaries for both chapters are present.
    assert "Ruth cuts clones" in blob and "county queue" in blob
    # Comps line from premise is present.
    assert "Train Dreams" in blob
    # No chapter body prose leaks in (invariant 4 regression guard).
    assert "rockwool cubes" not in blob
    assert "nobody summarizes" not in blob


def test_blurb_context_excludes_corpus_prose(tmp_path: Path):
    from stoner.ship.blurbs import build_blurb_context

    project = corpus_copy(tmp_path)
    ctx = build_blurb_context(project)
    blob = "\n".join(ctx.values())
    # A verbatim ch-01 body sentence must not appear anywhere in the context.
    assert "just below a node, the way she had been cutting them since 1986" not in blob


def test_blurbs_provider_error_leaves_no_partial_file(tmp_path: Path):
    from stoner.ship.blurbs import run_blurbs

    project = _blurb_project(tmp_path)
    with pytest.raises(ProviderError):
        run_blurbs(project, only="synopsis", provider=BoomProvider())
    assert not (project.root / "export" / "synopsis.md").exists()
