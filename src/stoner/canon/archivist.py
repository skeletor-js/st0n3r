"""The consistency engine: extract facts from a draft, diff vs. canon, apply.

This module is pure assemble/parse/diff/apply -- it never calls a provider
itself (see `docs/planning/ARCHITECTURE.md`: pipelines own the LLM call).
That keeps it fully unit-testable: feed it canned model output and assert
on the structured result.

Flow (owned by `pipelines/write.py`, not here):
    prompt = extract_facts_prompt(chapter_text, store.context_pack())
    text = provider.complete(...)
    parsed = parse_archivist_json(text)
    result = apply_updates(store, memory, parsed, chapter_number, auto=...)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..types import Severity
from .memory import Memory
from .store import CanonStore, slugify

_FACT_KINDS = {"character", "world", "timeline", "thread"}
_AGE_TOLERANCE = 1  # birthdays: off-by-one across a chapter isn't a conflict


class ArchivistError(RuntimeError):
    """Raised when model output cannot be parsed into the expected schema."""


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

_SCHEMA_INSTRUCTIONS = """\
Respond with STRICT JSON only -- no prose before or after, no markdown
fences unless you wrap the whole response in a single ```json block. The
JSON must match this shape exactly:

{
  "summary": "<2-4 sentence summary of what happened in this chapter>",
  "facts": [
    {
      "entity": "<character or world-entry name as it appears in canon>",
      "kind": "character|world|timeline|thread",
      "field": "<frontmatter field this fact updates, e.g. age, status, eyes>",
      "value": "<the new/confirmed value>",
      "quote": "<short verbatim quote from the chapter supporting this fact>"
    }
  ],
  "new_entities": [
    {"name": "<name>", "kind": "character|world", "reason": "<why this is new>"}
  ],
  "thread_updates": [
    {"id": "<existing thread id from threads.md>", "status": "open|resolved|abandoned", "note": "<why>"}
  ]
}

Rules:
- Only report facts that are stated or strongly implied by the chapter text.
- "facts" is for durable, checkable attributes -- not plot summary.
- Use "timeline" kind for events worth a timeline.md row (field="event").
- Never invent a thread id; only update ids that already exist in canon.
- If nothing applies to a list, return an empty list for it.
"""


def extract_facts_prompt(chapter_text: str, canon_digest: str) -> str:
    """Build the archivist prompt: canon context + chapter + JSON schema ask."""
    return (
        "You are the continuity archivist for a novel-writing harness. Read "
        "the chapter below against the existing canon and extract facts, "
        "new entities, and thread status changes.\n\n"
        "## Canon (current state)\n\n"
        f"{canon_digest.strip()}\n\n"
        "## Chapter Text\n\n"
        f"{chapter_text.strip()}\n\n"
        "## Task\n\n"
        f"{_SCHEMA_INSTRUCTIONS}"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_archivist_json(text: str) -> dict[str, Any]:
    """Tolerantly extract the archivist JSON object from raw model output.

    Tries, in order: the whole string as JSON, a fenced ```json block, a
    fenced ``` block, then the first balanced-looking `{...}` span. Raises
    `ArchivistError` if nothing parses.
    """
    candidates: list[str] = [text.strip()]

    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates.extend(f.strip() for f in fenced)

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first : last + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return _normalize(parsed)
    raise ArchivistError("could not find valid JSON in archivist output")


def _normalize(parsed: dict[str, Any]) -> dict[str, Any]:
    parsed.setdefault("summary", "")
    parsed.setdefault("facts", [])
    parsed.setdefault("new_entities", [])
    parsed.setdefault("thread_updates", [])
    return parsed


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


@dataclass
class Conflict:
    entity: str
    field: str
    canon_value: Any
    new_value: Any
    quote: str = ""
    severity: Severity = Severity.major


def _looks_numeric(*values: Any) -> bool:
    for v in values:
        try:
            float(str(v))
        except (TypeError, ValueError):
            return False
    return True


def _values_conflict(field_name: str, canon_value: Any, new_value: Any) -> bool:
    if canon_value is None or new_value is None or new_value == "":
        return False
    if "age" in field_name.lower() and _looks_numeric(canon_value, new_value):
        return abs(float(canon_value) - float(new_value)) > _AGE_TOLERANCE
    return str(canon_value).strip().lower() != str(new_value).strip().lower()


def _severity_for(field_name: str) -> Severity:
    if field_name.lower() == "status":
        return Severity.critical
    if field_name.lower() in {"age", "eyes", "hair", "build", "role", "type"}:
        return Severity.major
    return Severity.minor


def diff_against_canon(facts: list[dict[str, Any]], store: CanonStore) -> list[Conflict]:
    """Flag facts whose value materially contradicts canon frontmatter.

    A fact only conflicts when canon already has *some* value for that
    field and the new value differs beyond tolerance -- a fact about a
    field canon has never recorded is a new addition, not a conflict.
    """
    conflicts: list[Conflict] = []
    for fact in facts:
        kind = fact.get("kind")
        entity = fact.get("entity") or ""
        field_name = fact.get("field") or ""
        new_value = fact.get("value")
        if kind not in ("character", "world") or not entity or not field_name:
            continue
        finder = store.find_character_by_name if kind == "character" else store.find_world_by_name
        entry = finder(entity)
        if entry is None:
            continue
        canon_value = entry.frontmatter.get(field_name)
        if _values_conflict(field_name, canon_value, new_value):
            conflicts.append(
                Conflict(
                    entity=entity,
                    field=field_name,
                    canon_value=canon_value,
                    new_value=new_value,
                    quote=fact.get("quote", ""),
                    severity=_severity_for(field_name),
                )
            )
    return conflicts


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """Report of what `apply_updates` did (or would do, if `dry_run`)."""

    chapter: int
    dry_run: bool
    applied_facts: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    new_entities: list[dict[str, Any]] = field(default_factory=list)
    thread_updates: list[dict[str, Any]] = field(default_factory=list)
    summary_saved: bool = False


def apply_updates(
    store: CanonStore,
    memory: Memory,
    parsed: dict[str, Any],
    chapter_number: int,
    auto: bool = False,
) -> ApplyResult:
    """Merge non-conflicting facts into canon, record conflicts for review.

    `auto=False` (default) computes and returns exactly what *would*
    happen without touching disk -- useful for a CLI/UI review step before
    committing. `auto=True` performs the writes. Canon is never
    auto-overwritten for a field already in conflict; those are always
    surfaced in `ApplyResult.conflicts` for a human to resolve.
    """
    facts = parsed.get("facts", [])
    conflicts = diff_against_canon(facts, store)
    conflict_keys = {(c.entity, c.field) for c in conflicts}

    result = ApplyResult(chapter=chapter_number, dry_run=not auto, conflicts=conflicts)

    for fact in facts:
        kind = fact.get("kind")
        entity = fact.get("entity") or ""
        field_name = fact.get("field") or ""
        value = fact.get("value")
        if kind not in _FACT_KINDS or not entity or not field_name:
            continue
        if (entity, field_name) in conflict_keys:
            continue

        if kind == "character":
            if auto:
                existing = store.find_character_by_name(entity)
                slug = existing.slug if existing else slugify(entity)
                store.upsert_character(slug, {field_name: value})
            result.applied_facts.append(fact)
        elif kind == "world":
            if auto:
                existing = store.find_world_by_name(entity)
                slug = existing.slug if existing else slugify(entity)
                store.upsert_world(slug, {field_name: value})
            result.applied_facts.append(fact)
        elif kind == "timeline" and field_name in ("event", "summary"):
            if auto:
                store.add_timeline_row(
                    when=f"ch-{chapter_number:02d}",
                    event=str(value),
                    chapters=str(chapter_number),
                    characters=entity,
                )
            result.applied_facts.append(fact)
        # kind == "thread" facts are ignored here; thread_updates below is
        # the authoritative channel for thread status changes.

    existing_thread_ids = {t.id for t in store.threads()}
    for update in parsed.get("thread_updates", []):
        thread_id = update.get("id")
        status = update.get("status")
        note = update.get("note", "")
        if not thread_id or thread_id not in existing_thread_ids:
            result.thread_updates.append({**update, "applied": False, "reason": "unknown thread id"})
            continue
        if auto:
            fields: dict[str, Any] = {}
            if status:
                fields["status"] = status
            if note:
                fields["notes"] = note
            if fields:
                store.update_thread(thread_id, **fields)
        result.thread_updates.append({**update, "applied": auto})

    result.new_entities = list(parsed.get("new_entities", []))

    summary = parsed.get("summary", "")
    if summary and auto:
        memory.set_chapter_summary(chapter_number, summary=summary, new_facts=facts)
        memory.rebuild_book_so_far()
        result.summary_saved = True

    return result
