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
from .store import PROMISE_KINDS, CanonStore, slugify

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
      "field": "<frontmatter field this fact updates, e.g. age, status, appearance.eyes -- use dotted paths for nested fields>",
      "value": "<the new/confirmed value>",
      "quote": "<short verbatim quote from the chapter supporting this fact>"
    }
  ],
  "new_entities": [
    {"name": "<name>", "kind": "character|world", "reason": "<why this is new>"}
  ],
  "thread_updates": [
    {"id": "<existing thread id from threads.md>", "status": "open|resolved|abandoned", "note": "<why>"}
  ],
  "planted_threads": [
    {"id": "<new thread id in the same style as existing ids>", "thread": "<what promise/question this chapter plants>", "kind": "mystery|threat|want|image", "quote": "<short verbatim quote where it is planted>"}
  ]
}

Rules:
- Only report facts that are stated or strongly implied by the chapter text.
- "facts" is for durable, checkable attributes -- not plot summary.
- Use "timeline" kind for events worth a timeline.md row (field="event").
- Never invent a thread id in "thread_updates"; only update ids that already exist in canon.
- "planted_threads" is ONLY for promises this chapter explicitly plants: a
  mystery the reader now wants answered, a threat that must be discharged, a
  want a character now visibly pursues, or an image/line clearly set up to
  pay off later. Do NOT log every plot beat -- only deliberate promises.
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
    parsed.setdefault("planted_threads", [])
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


def _find_field(frontmatter: dict[str, Any], field_name: str) -> tuple[str | None, Any]:
    """Locate a fact's field at the top level or one level deep.

    Character/world templates nest hard facts (e.g. `appearance: {eyes: gray}`),
    while the model may report either `eyes` or `appearance.eyes`. Returns
    (dotted_path, value) for wherever the field actually lives, or
    (None, None) if canon has never recorded it.
    """
    if "." in field_name:
        head, _, tail = field_name.partition(".")
        sub = frontmatter.get(head)
        if isinstance(sub, dict) and tail in sub:
            return field_name, sub[tail]
        return None, None
    if field_name in frontmatter:
        return field_name, frontmatter[field_name]
    for key, sub in frontmatter.items():
        if isinstance(sub, dict) and field_name in sub:
            return f"{key}.{field_name}", sub[field_name]
    return None, None


def _nested_update(
    frontmatter: dict[str, Any], field_name: str, value: Any
) -> dict[str, Any]:
    """Build a shallow-merge-safe frontmatter update for a possibly-nested field.

    Nested updates copy the existing sub-mapping so a shallow merge doesn't
    clobber sibling keys (e.g. updating appearance.eyes must keep
    appearance.hair).
    """
    path, _existing = _find_field(frontmatter, field_name)
    target = path or field_name
    if "." in target:
        head, _, tail = target.partition(".")
        sub = frontmatter.get(head)
        merged = dict(sub) if isinstance(sub, dict) else {}
        merged[tail] = value
        return {head: merged}
    return {target: value}


def _values_conflict(field_name: str, canon_value: Any, new_value: Any) -> bool:
    if canon_value is None or new_value is None or new_value == "":
        return False
    if "age" in field_name.lower() and _looks_numeric(canon_value, new_value):
        return abs(float(canon_value) - float(new_value)) > _AGE_TOLERANCE
    return str(canon_value).strip().lower() != str(new_value).strip().lower()


def _severity_for(field_name: str) -> Severity:
    leaf = field_name.rsplit(".", 1)[-1].lower()
    if leaf == "status":
        return Severity.critical
    if leaf in {"age", "eyes", "hair", "build", "role", "type"}:
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
        _path, canon_value = _find_field(entry.frontmatter, field_name)
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
    planted_threads: list[dict[str, Any]] = field(default_factory=list)
    summary_saved: bool = False


def _norm_desc(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _apply_plants(
    store: CanonStore,
    parsed: dict[str, Any],
    when: str,
    auto: bool,
    result: ApplyResult,
) -> None:
    """Append newly planted promises as open, kind-typed thread rows.

    Idempotent like timeline rows: a plant whose normalized description
    already exists in threads.md is a no-op (surfaced, not re-applied), so
    re-archiving a redraft never double-plants. A proposed id that collides
    with a *different* existing thread, or an unrecognized kind, is surfaced
    unapplied for a human -- never auto-overwritten (the Conflict discipline).
    """
    threads = store.threads()
    existing_ids = {t.id for t in threads}
    existing_descs = {_norm_desc(t.thread) for t in threads}
    for plant in parsed.get("planted_threads", []):
        pid = str(plant.get("id") or "").strip()
        desc = str(plant.get("thread") or "").strip()
        kind = str(plant.get("kind") or "").strip().lower()
        if not pid or not desc:
            continue
        if kind not in PROMISE_KINDS:
            result.planted_threads.append(
                {**plant, "applied": False, "reason": f"invalid promise kind: {kind or '(empty)'}"}
            )
            continue
        ndesc = _norm_desc(desc)
        if ndesc in existing_descs:
            result.planted_threads.append(
                {**plant, "applied": False, "reason": "already planted"}
            )
            continue
        if pid in existing_ids:
            result.planted_threads.append(
                {**plant, "applied": False, "reason": "id collides with an existing thread"}
            )
            continue
        if auto:
            store.plant_promise(pid, desc, kind, opened_in=when)
        existing_ids.add(pid)
        existing_descs.add(ndesc)
        result.planted_threads.append({**plant, "applied": auto, "opened_in": when})


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
                updates = _nested_update(existing.frontmatter if existing else {}, field_name, value)
                store.upsert_character(slug, updates)
            result.applied_facts.append(fact)
        elif kind == "world":
            if auto:
                existing = store.find_world_by_name(entity)
                slug = existing.slug if existing else slugify(entity)
                updates = _nested_update(existing.frontmatter if existing else {}, field_name, value)
                store.upsert_world(slug, updates)
            result.applied_facts.append(fact)
        elif kind == "timeline" and field_name in ("event", "summary"):
            when = f"ch-{chapter_number:02d}"
            # Re-running the archivist on the same chapter (routine during
            # redrafts) must not duplicate timeline rows.
            duplicate = any(
                r.when == when and r.event.strip().lower() == str(value).strip().lower()
                for r in store.timeline_rows()
            )
            if auto and not duplicate:
                store.add_timeline_row(
                    when=when,
                    event=str(value),
                    chapters=str(chapter_number),
                    characters=entity,
                )
            result.applied_facts.append(fact)
        # kind == "thread" facts are ignored here; thread_updates below is
        # the authoritative channel for thread status changes.

    when = f"ch-{chapter_number:02d}"
    by_id = {t.id: t for t in store.threads()}
    for update in parsed.get("thread_updates", []):
        thread_id = update.get("id")
        status = update.get("status")
        note = update.get("note", "")
        row = by_id.get(thread_id) if thread_id else None
        if row is None:
            result.thread_updates.append({**update, "applied": False, "reason": "unknown thread id"})
            continue
        # A payoff never overwrites a row already resolved in a different
        # chapter -- surface it for a human, mirroring the Conflict discipline.
        if (
            status == "resolved"
            and row.status == "resolved"
            and row.resolved_in
            and row.resolved_in != when
        ):
            result.thread_updates.append(
                {**update, "applied": False, "reason": f"already resolved in {row.resolved_in}"}
            )
            continue
        fields: dict[str, Any] = {}
        if status:
            fields["status"] = status
        if note:
            fields["notes"] = note
        # Stamp the payoff chapter only when resolving a row that lacks one.
        if status == "resolved" and not row.resolved_in:
            fields["resolved_in"] = when
        if auto and fields:
            store.update_thread(thread_id, **fields)
        result.thread_updates.append({**update, "applied": auto})

    _apply_plants(store, parsed, when, auto, result)

    result.new_entities = list(parsed.get("new_entities", []))

    summary = parsed.get("summary", "")
    if summary and auto:
        memory.set_chapter_summary(chapter_number, summary=summary, new_facts=facts)
        memory.rebuild_book_so_far()
        result.summary_saved = True

    return result
