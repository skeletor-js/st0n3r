"""Tests for the facts research pipeline (U4). No network, no sockets.

A scripted FakeProvider stands in for a real backend (test_review.py
pattern); the fetch path's web_fetch goes through httpx.MockTransport.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.facts.locker import apply_facts
from stoner.facts.research import FactsDisabledError, run_research
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.types import CompletionRequest, CompletionResponse, ToolCall, Usage

HUMBOLDT_JSON = json.dumps(
    {
        "facts": [
            {
                "name": "Cannabis Land Use Ordinance",
                "claim": "Humboldt's Cannabis Land Use Ordinance passed in 2016 and was amended in 2019 to cap canopy.",
                "source_url": "https://humboldtgov.org/cluo",
                "source_title": "Humboldt CLUO",
                "quote": "The ordinance passed in 2016 ... amended in 2019.",
                "tags": ["permits", "humboldt"],
                "confidence": "high",
            }
        ]
    }
)


class FakeProvider(Provider):
    name = "fake"

    def __init__(
        self,
        responses: list | Callable[[int], CompletionResponse],
        *,
        supports_web_search: bool = False,
        supports_tools: bool = True,
    ):
        self.responses = responses
        self.supports_web_search = supports_web_search
        self.supports_tools = supports_tools
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


@pytest.fixture
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Test Book")
    scaffold_project(proj, "Test Book")
    proj.config.facts.enabled = True  # opt in for these tests
    return proj


def _ledger_actions(project: WritingProject) -> list[str]:
    path = project.root / ".stoner" / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln)["action"] for ln in path.read_text().splitlines() if ln.strip()]


def _native(text: str, web_searches: int = 2) -> FakeProvider:
    resp = CompletionResponse(
        text=text,
        stop_reason="end",
        usage=Usage(input_tokens=100, output_tokens=50, web_searches=web_searches),
        raw={"web_search_queries": ["humboldt cluo"], "web_search_urls": ["https://humboldtgov.org/cluo"]},
    )
    return FakeProvider([resp], supports_web_search=True)


# ---------------------------------------------------------------------------
# opt-in gate + book-mode refusal
# ---------------------------------------------------------------------------


def test_disabled_refuses_before_any_call(tmp_path: Path):
    proj = WritingProject.create(tmp_path / "b", "B")
    scaffold_project(proj, "B")
    # facts.enabled defaults False.
    provider = _native(HUMBOLDT_JSON)
    with pytest.raises(FactsDisabledError, match="facts.enabled"):
        run_research(proj, "anything", provider=provider)
    assert provider.requests == []
    assert _ledger_actions(proj) == []


def test_book_context_refuses(project: WritingProject):
    provider = _native(HUMBOLDT_JSON)
    with pytest.raises(FactsDisabledError, match="book mode"):
        run_research(project, "topic", provider=provider, book_context=True)
    assert provider.requests == []
    assert not any(a.startswith("facts.") for a in _ledger_actions(project))


# ---------------------------------------------------------------------------
# native path
# ---------------------------------------------------------------------------


def test_native_dry_run_lists_candidates(project: WritingProject):
    provider = _native(HUMBOLDT_JSON)
    res = run_research(project, "humboldt permits", provider=provider)
    assert res.path == "native"
    assert res.dry_run is True
    assert len(res.candidates) == 1
    assert res.applied  # would-apply list populated
    # dry-run writes nothing
    assert not (project.root / "canon/facts/cannabis-land-use-ordinance.md").exists()


def test_native_apply_writes_and_ledgers_in_order(project: WritingProject):
    provider = _native(HUMBOLDT_JSON)
    res = run_research(project, "humboldt permits", provider=provider, apply=True)
    assert (project.root / "canon/facts/cannabis-land-use-ordinance.md").exists()
    actions = _ledger_actions(project)
    # research.start -> search -> apply -> research.done, in order
    assert actions.index("facts.research.start") < actions.index("facts.search")
    assert actions.index("facts.search") < actions.index("facts.apply")
    assert actions.index("facts.apply") < actions.index("facts.research.done")
    assert res.usage.web_searches == 2


# ---------------------------------------------------------------------------
# fetch path
# ---------------------------------------------------------------------------


def test_fetch_path_applies_and_ledgers_url(project: WritingProject):
    seed = "https://humboldtgov.org/cluo"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><p>The ordinance passed in 2016.</p></body></html>",
        )

    transport = httpx.MockTransport(handler)

    def responses(idx: int) -> CompletionResponse:
        if idx == 0:
            return CompletionResponse(
                tool_calls=[ToolCall(name="web_fetch", arguments={"url": seed})],
                stop_reason="tool_use",
                usage=Usage(input_tokens=20, output_tokens=10),
            )
        return CompletionResponse(text=HUMBOLDT_JSON, stop_reason="end", usage=Usage())

    provider = FakeProvider(responses, supports_web_search=False, supports_tools=True)
    res = run_research(
        project, "humboldt permits", urls=(seed,), provider=provider, apply=True, fetch_transport=transport
    )
    assert res.path == "fetch"
    assert res.applied
    actions = _ledger_actions(project)
    assert "facts.fetch" in actions
    lines = [
        json.loads(ln)
        for ln in (project.root / ".stoner/ledger.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    fetch_line = next(ln for ln in lines if ln["action"] == "facts.fetch")
    assert seed in fetch_line["target"]


# ---------------------------------------------------------------------------
# no-capability path
# ---------------------------------------------------------------------------


def test_no_capability_fails_with_three_remedies(project: WritingProject):
    provider = FakeProvider([CompletionResponse(text="x")], supports_web_search=False, supports_tools=False)
    with pytest.raises(FactsDisabledError) as exc:
        run_research(project, "topic", provider=provider)
    msg = str(exc.value)
    assert "Anthropic" in msg and "claude" in msg and "--url" in msg


# ---------------------------------------------------------------------------
# unsourced drop + conflict
# ---------------------------------------------------------------------------


def test_unsourced_candidate_dropped_and_counted(project: WritingProject):
    payload = json.dumps(
        {"facts": [{"name": "Bare", "claim": "no source here", "confidence": "low"}]}
    )
    provider = _native(payload)
    res = run_research(project, "topic", provider=provider, apply=True)
    assert res.applied == []
    assert res.skipped_unsourced == 1
    assert any("unsourced" in n for n in res.notes)
    assert not (project.root / "canon/facts/bare.md").exists()


def test_conflicting_candidate_not_written(project: WritingProject):
    store = CanonStore(project)
    apply_facts(
        store,
        [
            {
                "name": "Cannabis Land Use Ordinance",
                "claim": "The CLUO passed in 2016.",
                "source_url": "https://humboldtgov.org/cluo",
                "confidence": "high",
            }
        ],
        auto=True,
    )
    contradiction = json.dumps(
        {
            "facts": [
                {
                    "name": "Cannabis Land Use Ordinance",
                    "claim": "The CLUO passed in 2011.",
                    "source_url": "https://example.com/wrong",
                    "confidence": "low",
                }
            ]
        }
    )
    provider = _native(contradiction)
    res = run_research(project, "topic", provider=provider, apply=True)
    assert len(res.conflicts) == 1
    assert res.applied == []
    # Original untouched
    entry = store.get_fact("cannabis-land-use-ordinance")
    assert entry is not None
    assert "2016" in entry.frontmatter["claim"]
    assert "facts.conflict" in _ledger_actions(project)


# ---------------------------------------------------------------------------
# researcher role resolution
# ---------------------------------------------------------------------------


def test_researcher_role_used_when_set(project: WritingProject):
    project.config.models.researcher = "anthropic/claude-researcher-x"
    provider = _native(HUMBOLDT_JSON)
    run_research(project, "topic", provider=provider)
    assert provider.requests[0].model == "claude-researcher-x"


def test_researcher_falls_back_to_writer_when_empty(project: WritingProject):
    project.config.models.researcher = ""
    project.config.models.writer = "anthropic/claude-writer-y"
    provider = _native(HUMBOLDT_JSON)
    run_research(project, "topic", provider=provider)
    assert provider.requests[0].model == "claude-writer-y"
