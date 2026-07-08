"""The `facts research` pipeline: opt-in web research into the locker.

Owns the LLM call (the locker never does). Flow:

1. Refuse unless `facts.enabled` (actionable message naming the yaml key).
2. Refuse in autonomous book-mode context even when enabled -- an explicit
   guard, not mere unwiring, so the network surface stays strictly
   human-invoked. Book mode consumes the locker; it never builds it.
3. Resolve the researcher role (`models.researcher`, else the writer).
4. Pick the web-capable path:
   (a) provider-native search (`provider.supports_web_search`) -- one
       `complete()` with a `WebSearchSpec` built from `FactsConfig`;
   (b) harness-side `web_fetch` when the provider supports tools and seeds or
       existing locker sources give starting URLs -- an agent loop;
   (c) none -- fail with an actionable message naming the three remedies.
5. Parse candidates tolerantly, drop any without a `source_url` (counted),
   diff against the locker, and apply per the canon method (dry-run default).

Every network action is ledgered under `facts.*`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..canon.store import CanonStore
from ..config import StonerConfig
from ..engine.agent import Agent
from ..engine.tools import ToolRegistry, query_canon, search_text
from ..ledger import Ledger
from ..pipelines.common import render_prompt
from ..project import WritingProject
from ..providers.base import Provider
from ..providers.registry import get_provider, parse_model_string
from ..types import CompletionRequest, Message, ToolSpec, Usage, WebSearchSpec
from . import locker
from .locker import FactConflict, FactRecord
from .webfetch import make_web_fetch_tool

_FETCH_MAX_TURNS = 12


class FactsDisabledError(RuntimeError):
    """Raised when research is invoked without the opt-in flag or in book mode."""


@dataclass
class ResearchResult:
    """What a research run produced (or would, in dry-run)."""

    topic: str
    path: str  # "native" | "fetch"
    dry_run: bool
    candidates: list[dict[str, Any]] = field(default_factory=list)
    applied: list[FactRecord] = field(default_factory=list)
    conflicts: list[FactConflict] = field(default_factory=list)
    skipped_unsourced: int = 0
    usage: Usage = field(default_factory=Usage)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _system_prompt(project: WritingProject) -> str:
    return render_prompt("facts_research.md", {"project_name": project.config.project_name})


def _user_prompt(topic: str, urls: tuple[str, ...], digest: str, *, native: bool) -> str:
    parts = [f"## Research topic\n\n{topic.strip()}"]
    if digest:
        parts.append(
            "## Facts already in the locker\n\n"
            "Do not duplicate these; only return one if you have a materially "
            "better or contradicting source.\n\n" + digest
        )
    if urls:
        parts.append("## Seed URLs (start here)\n\n" + "\n".join(f"- {u}" for u in urls))
    if native:
        parts.append(
            "Use web search to find authoritative primary sources for the topic, "
            "then respond with the STRICT JSON facts block. Cite the exact URL each "
            "claim came from."
        )
    else:
        parts.append(
            "Use the `web_fetch` tool to read the seed URLs (and any other URLs you "
            "are confident exist) and quote from them. You CANNOT discover new pages "
            "by search on this path -- work only from URLs you are given or already "
            "know. When done, respond with the STRICT JSON facts block."
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Path selection helpers
# ---------------------------------------------------------------------------


def _resolve_researcher(
    config: StonerConfig, model: str | None, provider: Provider | None
) -> tuple[Provider, str, str]:
    """Return (provider, bare model id, full model string) for the researcher.

    `models.researcher` resolves against the writer role when empty. A given
    `provider` overrides construction (test injection) but the model string is
    still resolved for its bare id.
    """
    model_str = model or config.models.researcher or config.models.writer
    if provider is not None:
        model_id = parse_model_string(model_str)[1] if "/" in model_str else model_str
        return provider, model_id, model_str
    prov, model_id = get_provider(model_str, config)
    return prov, model_id, model_str


def _no_capability_message() -> str:
    return (
        "No web-capable path is available for `facts research`. To get one:\n"
        "  1. Set the researcher role to an Anthropic API model "
        "(models.researcher: anthropic/claude-sonnet-5) for native web search;\n"
        "  2. or use the `claude` provider (models.researcher: claude/claude-sonnet-5), "
        "which has built-in WebSearch;\n"
        "  3. or pass --url seeds with a tool-capable provider to fetch known pages "
        "(verification, not discovery)."
    )


# ---------------------------------------------------------------------------
# The two paths
# ---------------------------------------------------------------------------


def _run_native(
    project: WritingProject,
    provider: Provider,
    model_id: str,
    topic: str,
    urls: tuple[str, ...],
    digest: str,
    ledger: Ledger,
) -> tuple[str, Usage]:
    spec = WebSearchSpec(
        max_uses=project.config.facts.max_searches,
        allowed_domains=list(project.config.facts.allowed_domains),
    )
    req = CompletionRequest(
        model=model_id,
        system=_system_prompt(project),
        messages=[Message(role="user", content=_user_prompt(topic, urls, digest, native=True))],
        max_tokens=project.config.max_tokens,
        temperature=project.config.temperature,
        web_search=spec,
    )
    resp = provider.complete(req)
    raw = resp.raw or {}
    queries = raw.get("web_search_queries") or []
    note = raw.get("web_search_note") or ""
    ledger.append(
        "facts.search",
        target=topic,
        count=resp.usage.web_searches,
        queries=[str(q) for q in queries][:20],
        urls=[str(u) for u in (raw.get("web_search_urls") or [])][:20],
        note=note or ("count unknown" if resp.usage.web_searches == 0 and note else ""),
    )
    return resp.text, resp.usage


def _run_fetch(
    project: WritingProject,
    provider: Provider,
    model_id: str,
    topic: str,
    urls: tuple[str, ...],
    digest: str,
    ledger: Ledger,
    transport: httpx.BaseTransport | None,
) -> tuple[str, Usage]:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="web_fetch",
            description=(
                "Fetch one URL and return its readable text. Only fetch URLs you were "
                "given or are confident exist; this tool cannot search or discover pages."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        ),
        make_web_fetch_tool(project, ledger, project.config, transport=transport),
    )
    reg.register(
        ToolSpec(
            name="query_canon",
            description="List canon files, or read one by topic/slug.",
            parameters={"type": "object", "properties": {"topic": {"type": "string", "default": ""}}},
        ),
        query_canon,
    )
    reg.register(
        ToolSpec(
            name="search_text",
            description="Search project markdown for a substring; returns path:line matches.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scope": {"type": "string", "default": "all"},
                },
                "required": ["query"],
            },
        ),
        search_text,
    )
    agent = Agent(provider, model_id, reg, project, ledger, session_name="facts-research")
    result = agent.run(
        task=_user_prompt(topic, urls, digest, native=False),
        system=_system_prompt(project),
        max_turns=_FETCH_MAX_TURNS,
        max_tokens_budget=project.config.max_tokens * _FETCH_MAX_TURNS,
    )
    return result.text, result.usage


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_research(
    project: WritingProject,
    topic: str,
    *,
    urls: tuple[str, ...] = (),
    apply: bool = False,
    model: str | None = None,
    provider: Provider | None = None,
    book_context: bool = False,
    fetch_transport: httpx.BaseTransport | None = None,
) -> ResearchResult:
    """Research `topic` and diff/apply the results into the fact locker.

    `apply=False` (default) is dry-run: candidates and conflicts are computed
    but nothing is written. Raises `FactsDisabledError` when `facts.enabled`
    is false or when called from book-mode context -- in both cases before any
    provider call, so nothing network-touching happens and nothing is ledgered.
    """
    config = project.config
    if not config.facts.enabled:
        raise FactsDisabledError(
            "Web research is off. Set `facts.enabled: true` in stoner.yaml to allow "
            "network access (it is opt-in), then re-run `stoner facts research`."
        )
    if book_context:
        raise FactsDisabledError(
            "`facts research` cannot run in autonomous book mode: book mode consumes "
            "the locker, it never builds it. Run `stoner facts research` yourself to "
            "add sourced facts before drafting."
        )

    store = CanonStore(project)
    ledger = Ledger(project.root)
    digest = locker.facts_digest(store)

    prov, model_id, model_str = _resolve_researcher(config, model, provider)

    # Fetch-path availability: existing locker sources also seed starting URLs.
    canon_urls = [fr.source_url for fr in locker.list_facts(store) if fr.source_url]
    if prov.supports_web_search:
        path = "native"
    elif getattr(prov, "supports_tools", False) and (urls or canon_urls):
        path = "fetch"
    else:
        raise FactsDisabledError(_no_capability_message())

    ledger.append("facts.research.start", target=topic, path=path, model=model_str)

    notes: list[str] = []
    if path == "native":
        text, usage = _run_native(project, prov, model_id, topic, urls, digest, ledger)
    else:
        notes.append("fetch mode: verification only (no search); worked from seed/known URLs")
        text, usage = _run_fetch(
            project, prov, model_id, topic, urls, digest, ledger, fetch_transport
        )

    candidates = locker.parse_research_json(text)
    sourced = [c for c in candidates if c.get("source_url")]
    dropped = len(candidates) - len(sourced)
    if dropped:
        notes.append(f"dropped {dropped} unsourced candidate(s) (no source_url)")
    max_facts = config.facts.max_facts_per_run
    if len(sourced) > max_facts:
        notes.append(f"capped at max_facts_per_run={max_facts} (had {len(sourced)})")
        sourced = sourced[:max_facts]

    apply_res = locker.apply_facts(store, sourced, auto=apply)

    for fr in apply_res.applied:
        ledger.append(
            "facts.apply",
            target=f"canon/facts/{fr.slug}.md",
            claim=fr.claim,
            source=fr.source_url,
            dry_run=not apply,
        )
    for cf in apply_res.conflicts:
        ledger.append(
            "facts.conflict",
            target=f"canon/facts/{cf.slug}.md",
            field=cf.field,
            locker_value=str(cf.locker_value),
            candidate=str(cf.new_value),
        )

    result = ResearchResult(
        topic=topic,
        path=path,
        dry_run=not apply,
        candidates=candidates,
        applied=apply_res.applied,
        conflicts=apply_res.conflicts,
        skipped_unsourced=dropped,
        usage=usage,
        notes=notes,
    )

    ledger.append(
        "facts.research.done",
        target=topic,
        path=path,
        candidates=len(candidates),
        applied=len(apply_res.applied),
        conflicts=len(apply_res.conflicts),
        dropped_unsourced=dropped,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        web_searches=usage.web_searches,
    )

    return result
