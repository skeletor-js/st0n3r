"""Re-locate prior notebook flags in revised chapter text.

Deterministic-first with ONE batched LLM fallback per call (invariant 2 and
the R17 cost bound). Tiers, in order:

  1. `locate_span` exact match -- the quote is verbatim-present;
  2. whitespace/curly-quote-normalized search, offsets mapped back to the
     original body;
  3. sentence-anchor match: the first and last ~5 words of the quote each
     located, the span covering both.

A deterministic hit means the flagged text is unchanged, so the item is
`persisting` (unchanged text means unaddressed -- assumption A2). Everything
that drifts beyond tier 3 goes into a single `room_relocate.md` completion
that classifies each item as RESOLVED or supplies a new verbatim quote. New
quotes are re-verified with `locate_span` before acceptance -- an
unverifiable "quote" from the model degrades to `unlocatable`, never trusted.
`llm_relocate: false` or a provider failure degrades every drifted item to
`unlocatable` (still open); this module never raises for provider trouble.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..pipelines.common import render_prompt
from ..project import WritingProject
from ..providers.base import Provider, ProviderError
from ..review.passes import extract_json, locate_span
from ..types import CompletionRequest, Message, Span, Usage

# Outcomes: persisting (quote found, possibly at a new span), resolved (text
# changed and the issue judged gone), unlocatable (cannot be re-found; stays
# open in the notebook).
_ANCHOR_WORDS = 5

_WS_RE = re.compile(r"\s+")
_QUOTE_TRANS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


@dataclass
class RelocationOutcome:
    item_id: str
    outcome: str  # persisting | resolved | unlocatable
    span: Span | None = None
    new_quote: str = ""
    tier: str = ""  # exact | normalized | anchor | llm | none


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace and straighten curly quotes, keeping a map from
    each normalized index back to the original-body offset."""
    out: list[str] = []
    idx_map: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            idx_map.append(i)
            prev_space = True
        else:
            out.append(ch.translate(_QUOTE_TRANS))
            idx_map.append(i)
            prev_space = False
    return "".join(out), idx_map


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text.translate(_QUOTE_TRANS)).strip()


def _tier2_normalized(body: str, quote: str) -> Span | None:
    norm_quote = _normalize(quote)
    if not norm_quote:
        return None
    norm_body, idx_map = _normalize_with_map(body)
    pos = norm_body.find(norm_quote)
    if pos == -1:
        return None
    start = idx_map[pos]
    end_norm = pos + len(norm_quote) - 1
    end = idx_map[end_norm] + 1 if end_norm < len(idx_map) else len(body)
    line = body.count("\n", 0, start) + 1
    return Span(start=start, end=end, line=line)


def _tier3_anchors(body: str, quote: str) -> Span | None:
    words = quote.split()
    if len(words) < _ANCHOR_WORDS * 2:
        return None
    head = " ".join(words[:_ANCHOR_WORDS])
    tail = " ".join(words[-_ANCHOR_WORDS:])
    head_span = locate_span(body, head)
    if head_span is None:
        return None
    tail_idx = body.find(tail, head_span.start)
    if tail_idx == -1:
        return None
    end = tail_idx + len(tail)
    # Guard against absurd spans (anchors matching far apart).
    if end - head_span.start > max(len(quote) * 3, 600):
        return None
    return Span(start=head_span.start, end=end, line=head_span.line)


def relocate_items(
    body: str,
    items: list[dict[str, Any]],
    project: WritingProject,
    provider: Provider | None = None,
    model_id: str = "",
    llm_relocate: bool | None = None,
) -> tuple[list[RelocationOutcome], Usage]:
    """Re-locate each notebook item's quote in `body`.

    Deterministic tiers run first and are free; drifted items go into at most
    ONE batched completion (when `llm_relocate` and a provider are given).
    Never raises for provider failure -- drifted items degrade to
    `unlocatable`. Returns (outcomes, usage).
    """
    use_llm = project.config.room.llm_relocate if llm_relocate is None else llm_relocate
    outcomes: list[RelocationOutcome] = []
    drifted: list[dict[str, Any]] = []

    for item in items:
        item_id = str(item.get("id", ""))
        quote = str(item.get("quote", ""))
        if not quote:
            outcomes.append(RelocationOutcome(item_id=item_id, outcome="unlocatable", tier="none"))
            continue
        span = locate_span(body, quote)
        if span is not None:
            outcomes.append(
                RelocationOutcome(item_id=item_id, outcome="persisting", span=span, tier="exact")
            )
            continue
        span = _tier2_normalized(body, quote)
        if span is not None:
            outcomes.append(
                RelocationOutcome(item_id=item_id, outcome="persisting", span=span, tier="normalized")
            )
            continue
        span = _tier3_anchors(body, quote)
        if span is not None:
            outcomes.append(
                RelocationOutcome(item_id=item_id, outcome="persisting", span=span, tier="anchor")
            )
            continue
        drifted.append(item)

    if not drifted:
        return outcomes, Usage()

    if not use_llm or provider is None:
        for item in drifted:
            outcomes.append(
                RelocationOutcome(item_id=str(item.get("id", "")), outcome="unlocatable", tier="none")
            )
        return outcomes, Usage()

    outcomes_llm, usage = _llm_relocate(body, drifted, project, provider, model_id)
    outcomes.extend(outcomes_llm)
    return outcomes, usage


def _llm_relocate(
    body: str,
    drifted: list[dict[str, Any]],
    project: WritingProject,
    provider: Provider,
    model_id: str,
) -> tuple[list[RelocationOutcome], Usage]:
    """ONE batched completion classifying every drifted item; fail-soft."""
    system = render_prompt("room_relocate.md", {"project_name": project.config.project_name})
    item_lines = [
        json.dumps({"id": str(it.get("id", "")), "quote": str(it.get("quote", "")), "issue": str(it.get("issue", ""))})
        for it in drifted
    ]
    user = (
        "## Revised chapter text\n\n"
        + body
        + "\n\n## Drifted flags (one JSON object per line)\n\n"
        + "\n".join(item_lines)
        + "\n\nClassify every flag per your instructions."
    )
    try:
        resp = provider.complete(
            CompletionRequest(
                model=model_id,
                system=system,
                messages=[Message(role="user", content=user)],
                max_tokens=project.config.max_tokens,
                temperature=project.config.temperature,
            )
        )
    except ProviderError:
        return (
            [
                RelocationOutcome(item_id=str(it.get("id", "")), outcome="unlocatable", tier="none")
                for it in drifted
            ],
            Usage(),
        )

    data = extract_json(resp.text)
    raw = data.get("items")
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("id"):
                by_id[str(entry["id"])] = entry

    outcomes: list[RelocationOutcome] = []
    for item in drifted:
        item_id = str(item.get("id", ""))
        entry = by_id.get(item_id)
        if entry is None:
            outcomes.append(RelocationOutcome(item_id=item_id, outcome="unlocatable", tier="llm"))
            continue
        verdict = str(entry.get("verdict", "")).strip().upper()
        if verdict == "RESOLVED":
            outcomes.append(RelocationOutcome(item_id=item_id, outcome="resolved", tier="llm"))
            continue
        new_quote = str(entry.get("quote", ""))
        span = locate_span(body, new_quote) if new_quote else None
        if span is None:
            # A hallucinated quote is not trusted: unlocatable, still open.
            outcomes.append(RelocationOutcome(item_id=item_id, outcome="unlocatable", tier="llm"))
        else:
            outcomes.append(
                RelocationOutcome(
                    item_id=item_id, outcome="persisting", span=span, new_quote=new_quote, tier="llm"
                )
            )
    return outcomes, resp.usage
