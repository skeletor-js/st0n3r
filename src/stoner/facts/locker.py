"""The fact locker: pure assemble/parse/diff/apply over `canon/facts/`.

Like `canon/archivist`, this module never calls a provider -- it takes canned
model output (or hand-built candidates) and returns structured results, so it
is fully unit-testable. The research pipeline (`facts/research.py`) owns the
LLM call and hands parsed candidates here.

The locker follows the canon method exactly: new facts write cleanly,
same-slug-different-claim candidates surface as conflicts for a human, and
nothing is auto-overwritten. Dry-run is the default. `source_url` is
mandatory: an unsourced candidate is dropped and counted, never stored, so
the harness can never silently degrade into remembering facts it cannot
attribute. A fact's body (verbatim quotes + notes) and its human-set
`status` are never machine-edited once the entry exists.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..canon.store import CanonStore, slugify
from ..types import Severity

_CONFIDENCE_LEVELS = ("high", "medium", "low")
_STATUS_LEVELS = ("unverified", "verified", "disputed")
_DIGEST_MAX_CHARS = 4000


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class FactRecord:
    """One parsed locker entry (frontmatter fields + body)."""

    slug: str
    name: str
    claim: str
    source_url: str
    source_title: str = ""
    tags: list[str] = field(default_factory=list)
    confidence: str = "medium"
    status: str = "unverified"
    accessed: str = ""
    quote: str = ""  # supporting passage (only set on freshly-parsed candidates)
    body: str = ""


@dataclass
class FactConflict:
    """A candidate whose slug matches an entry but whose claim differs."""

    slug: str
    field: str
    locker_value: Any
    new_value: Any
    quote: str = ""
    severity: Severity = Severity.major


@dataclass
class FactsApplyResult:
    """Report of what `apply_facts` did (or would do, if `dry_run`)."""

    dry_run: bool
    applied: list[FactRecord] = field(default_factory=list)
    conflicts: list[FactConflict] = field(default_factory=list)
    skipped_unsourced: int = 0


# ---------------------------------------------------------------------------
# Reading the locker
# ---------------------------------------------------------------------------


def _record_from_entry(entry: Any) -> FactRecord:
    fm = entry.frontmatter
    tags_raw = fm.get("tags")
    tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
    return FactRecord(
        slug=entry.slug,
        name=str(fm.get("name") or entry.slug),
        claim=str(fm.get("claim") or ""),
        source_url=str(fm.get("source_url") or ""),
        source_title=str(fm.get("source_title") or ""),
        tags=tags,
        confidence=str(fm.get("confidence") or "medium"),
        status=str(fm.get("status") or "unverified"),
        accessed=str(fm.get("accessed") or ""),
        body=entry.body,
    )


def list_facts(store: CanonStore) -> list[FactRecord]:
    """All locker entries as `FactRecord`s (template stem already excluded)."""
    return [_record_from_entry(e) for e in store.list_entries(kind="fact")]


def _domain(url: str) -> str:
    if not url:
        return ""
    netloc = urlparse(url).netloc
    return netloc[4:] if netloc.startswith("www.") else netloc


def facts_digest(store: CanonStore, max_chars: int = _DIGEST_MAX_CHARS) -> str:
    """One line per fact: `- [slug] claim (confidence; domain)`.

    Shared by `context_pack` (the writer's channel) and the sweep pass. The
    slug is included so a sweep contradiction finding can name the fact it
    refutes. Truncation is whole-line -- a fact never appears half-quoted.
    """
    lines: list[str] = []
    for fr in list_facts(store):
        if not fr.claim:
            continue
        domain = _domain(fr.source_url)
        meta = fr.confidence + (f"; {domain}" if domain else "")
        lines.append(f"- [{fr.slug}] {fr.claim} ({meta})")
    if not lines:
        return ""
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Parsing candidate facts from model output
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json_value(text: str) -> Any:
    """Tolerantly pull a JSON value (dict or list) out of raw model output.

    Mirrors `canon.archivist.parse_archivist_json`'s candidate strategy but
    accepts a top-level list of facts too. Returns None if nothing parses.
    """
    if not text or not text.strip():
        return None
    candidates: list[str] = [text.strip()]
    candidates.extend(m.strip() for m in _FENCE_RE.findall(text))
    for opener, closer in (("{", "}"), ("[", "]")):
        first, last = text.find(opener), text.rfind(closer)
        if first != -1 and last != -1 and last > first:
            candidates.append(text[first : last + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _norm_confidence(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in _CONFIDENCE_LEVELS else "medium"


def parse_research_json(text: str) -> list[dict[str, Any]]:
    """Tolerantly extract candidate facts from researcher output.

    Accepts either `{"facts": [...]}` or a bare list of fact objects. Each
    fact is normalized to the locker keys; missing keys become empty. Never
    raises -- unparseable output yields an empty list (the caller notes it).
    """
    parsed = _extract_json_value(text)
    if isinstance(parsed, dict):
        facts = parsed.get("facts")
    elif isinstance(parsed, list):
        facts = parsed
    else:
        facts = None
    if not isinstance(facts, list):
        return []
    out: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        tags_raw = f.get("tags")
        out.append(
            {
                "name": str(f.get("name") or "").strip(),
                "claim": str(f.get("claim") or "").strip(),
                "source_url": str(f.get("source_url") or "").strip(),
                "source_title": str(f.get("source_title") or "").strip(),
                "quote": str(f.get("quote") or "").strip(),
                "tags": [str(t) for t in tags_raw] if isinstance(tags_raw, list) else [],
                "confidence": _norm_confidence(f.get("confidence")),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _claim_conflict(old: str, new: str) -> bool:
    if not old or not new:
        return False
    return old.strip().lower() != new.strip().lower()


def diff_facts(candidates: list[dict[str, Any]], store: CanonStore) -> list[FactConflict]:
    """Flag candidates that contradict an existing locker entry.

    A candidate conflicts only when an entry with the same slug already
    exists and its `claim` materially differs (case-insensitive). A candidate
    whose slug is new is an addition, not a conflict.
    """
    conflicts: list[FactConflict] = []
    for c in candidates:
        name = str(c.get("name") or "")
        if not name:
            continue
        slug = slugify(name)
        existing = store.get_fact(slug)
        if existing is None:
            continue
        old_claim = str(existing.frontmatter.get("claim") or "")
        new_claim = str(c.get("claim") or "")
        if _claim_conflict(old_claim, new_claim):
            conflicts.append(
                FactConflict(
                    slug=slug,
                    field="claim",
                    locker_value=old_claim,
                    new_value=new_claim,
                    quote=str(c.get("quote") or ""),
                )
            )
    return conflicts


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _today() -> str:
    return datetime.date.today().isoformat()


def _body_from_quote(quote: str) -> str:
    q = quote.strip()
    quotes_block = f"> {q}\n" if q else ""
    return f"## Quotes\n\n{quotes_block}\n## Notes\n"


def apply_facts(
    store: CanonStore,
    candidates: list[dict[str, Any]],
    *,
    auto: bool = False,
    accessed: str | None = None,
) -> FactsApplyResult:
    """Write non-conflicting sourced facts into the locker (dry-run default).

    `auto=False` computes what *would* happen without touching disk.
    `auto=True` writes new entries and refreshes the frontmatter of existing
    same-claim entries -- but never their body or human-set `status`, and
    never a conflicting slug. Candidates without a `source_url` are dropped
    and counted in `skipped_unsourced`, never stored.
    """
    accessed = accessed or _today()
    conflicts = diff_facts(candidates, store)
    conflict_slugs = {c.slug for c in conflicts}
    result = FactsApplyResult(dry_run=not auto, conflicts=conflicts)

    for c in candidates:
        name = str(c.get("name") or "")
        claim = str(c.get("claim") or "")
        source_url = str(c.get("source_url") or "")
        if not name or not claim:
            continue
        if not source_url:
            result.skipped_unsourced += 1
            continue
        slug = slugify(name)
        if slug in conflict_slugs:
            continue

        tags_raw = c.get("tags")
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        record = FactRecord(
            slug=slug,
            name=name,
            claim=claim,
            source_url=source_url,
            source_title=str(c.get("source_title") or ""),
            tags=tags,
            confidence=_norm_confidence(c.get("confidence")),
            status="unverified",
            accessed=accessed,
            quote=str(c.get("quote") or ""),
        )

        if auto:
            existing = store.get_fact(slug)
            updates: dict[str, Any] = {
                "name": record.name,
                "claim": record.claim,
                "source_url": record.source_url,
                "source_title": record.source_title,
                "tags": record.tags,
                "confidence": record.confidence,
                "accessed": record.accessed,
            }
            if existing is None:
                # New entry: seed status + a body skeleton carrying the quote.
                updates["status"] = "unverified"
                body: str | None = _body_from_quote(record.quote)
            else:
                # Existing: refresh frontmatter only. Human status and the
                # hand-editable body are preserved (body=None => keep).
                body = None
            store.upsert_fact(slug, updates, body)

        result.applied.append(record)

    return result
