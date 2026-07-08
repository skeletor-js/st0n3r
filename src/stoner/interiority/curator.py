"""The cast curator: extract private-state updates from a chapter, diff, apply.

This mirrors `canon/archivist.py` exactly: it is pure assemble/parse/diff/apply
and never calls a provider -- `interiority/pipeline.py` owns the model call.
That keeps it fully unit-testable: feed it canned model output and assert on
the structured result.

Discipline (invariant 3 applied to private state): conflicts are surfaced,
never auto-overwritten. A want shift when wants are already set, a lie
exposure recorded in a different chapter, or a duplicate fact with a different
`learned_in` all become `CastConflict` records rather than silent
overwrites. Same-chapter re-runs (routine during redrafts) are idempotent.
`apply_cast_update(auto=False)` is a dry run that touches no disk.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .sheet import KnowledgeEntry, Lie, Refusal
from .store import CastStore


class CuratorError(RuntimeError):
    """Raised when model output cannot be parsed into the expected schema."""


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

_SCHEMA_INSTRUCTIONS = """\
Respond with STRICT JSON only -- no prose before or after, no markdown fences
unless you wrap the whole response in a single ```json block. The JSON is an
object keyed by character slug; each value has this shape exactly:

{
  "<character-slug>": {
    "new_knowledge": [
      {"fact": "<what they now know>", "how": "witnessed|told|inferred",
       "quote": "<short verbatim quote from the chapter>", "secret": false}
    ],
    "want_shift": {"stated": "<new stated want>", "real": "<new real want>"},
    "new_lies": [
      {"claim": "<the lie they tell>", "truth": "<the truth, or a k-id>",
       "audience": "everyone|<slug>"}
    ],
    "lie_updates": [
      {"id": "<existing l-id from the sheet>", "exposed": true, "quote": "<quote>"}
    ],
    "refusal_updates": [{"topic": "<what they won't discuss>", "reason": "<why>"}]
  }
}

Rules:
- Only report state the chapter states or strongly implies.
- `new_knowledge` is what a character LEARNS in this chapter -- not everything
  they already knew. The harness stamps `learned_in` with this chapter number.
- Use `want_shift` only when the chapter genuinely reveals or changes a want.
  Set it to null if nothing changed.
- Never invent a lie id in `lie_updates`; only reference ids that already
  exist on the sheet above. Omit it entirely if no lie changed.
- If a list does not apply, return an empty list (or null for `want_shift`).
- Only use character slugs that appear in the cast sheets above.
"""


def cast_update_prompt(chapter_text: str, sheets_digest: str) -> str:
    """Build the curator user prompt: current cast state + chapter + schema."""
    return (
        "You are the cast curator for a novel-writing harness. Read the chapter "
        "below against each character's current private state and report what "
        "changed: newly-learned knowledge, want shifts, new or exposed lies, "
        "and new refusals.\n\n"
        "## Cast Sheets (current private state)\n\n"
        f"{sheets_digest.strip() or '(no cast sheets)'}\n\n"
        "## Chapter Text\n\n"
        f"{chapter_text.strip()}\n\n"
        "## Task\n\n"
        f"{_SCHEMA_INSTRUCTIONS}"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_cast_update(text: str) -> dict[str, Any]:
    """Tolerantly extract the curator JSON object (keyed by slug) from output.

    Tries, in order: the whole string, fenced ``` blocks, then the first
    balanced-looking `{...}` span. Raises `CuratorError` if nothing parses.
    """
    candidates: list[str] = [text.strip()]
    candidates.extend(f.strip() for f in re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL))
    first, last = text.find("{"), text.rfind("}")
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
            return parsed
    raise CuratorError("could not find valid JSON in cast-update output")


# ---------------------------------------------------------------------------
# Diff + apply
# ---------------------------------------------------------------------------


@dataclass
class CastConflict:
    """A proposed update that clashes with existing sheet state; never applied."""

    slug: str
    kind: str  # want_shift | lie_exposure | duplicate_knowledge
    detail: str
    quote: str = ""


@dataclass
class CastApplyResult:
    """Report of what `apply_cast_update` did (or would do, if `dry_run`)."""

    chapter: int
    dry_run: bool
    applied: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[CastConflict] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


def _norm(text: str) -> str:
    return str(text or "").strip().lower()


def apply_cast_update(
    store: CastStore,
    parsed: dict[str, Any],
    chapter: int,
    auto: bool = False,
) -> CastApplyResult:
    """Diff parsed updates against each sheet and apply the non-conflicting ones.

    `auto=False` (default) computes exactly what *would* happen without
    touching disk. `auto=True` writes each modified sheet once. Conflicts are
    always surfaced for a human, never auto-resolved. Same-chapter re-runs are
    idempotent (no duplicate knowledge, no re-exposed lie).
    """
    result = CastApplyResult(chapter=chapter, dry_run=not auto)

    for slug, raw in parsed.items():
        if not isinstance(raw, dict):
            continue
        if not store.exists(slug):
            result.skipped.append({"slug": slug, "reason": "no cast sheet for slug"})
            continue
        sheet = store.load(slug)
        modified = False

        # -- new knowledge -------------------------------------------------
        for nk in raw.get("new_knowledge") or []:
            if not isinstance(nk, dict):
                continue
            fact = str(nk.get("fact") or "").strip()
            if not fact:
                continue
            match = next((e for e in sheet.knowledge if _norm(e.fact) == _norm(fact)), None)
            if match is not None:
                if match.learned_in == chapter:
                    result.skipped.append(
                        {"slug": slug, "reason": "duplicate knowledge (same chapter)", "fact": fact}
                    )
                else:
                    result.conflicts.append(
                        CastConflict(
                            slug=slug,
                            kind="duplicate_knowledge",
                            detail=f"fact already known since ch-{match.learned_in:02d}: {fact!r}",
                            quote=str(nk.get("quote") or ""),
                        )
                    )
                continue
            entry = KnowledgeEntry(
                id=store.next_knowledge_id(sheet),
                fact=fact,
                learned_in=chapter,
                how=str(nk.get("how") or "inferred"),
                source=str(nk.get("quote") or ""),
                secret=bool(nk.get("secret", False)),
            )
            sheet.knowledge.append(entry)
            modified = True
            result.applied.append({"slug": slug, "kind": "knowledge", "id": entry.id, "fact": fact})

        # -- want shift ----------------------------------------------------
        ws = raw.get("want_shift")
        if isinstance(ws, dict):
            new_stated = str(ws.get("stated") or "").strip()
            new_real = str(ws.get("real") or "").strip()
            if new_stated or new_real:
                if sheet.wants.is_set():
                    same = _norm(sheet.wants.stated) == _norm(new_stated) and _norm(
                        sheet.wants.real
                    ) == _norm(new_real)
                    if not same:
                        result.conflicts.append(
                            CastConflict(
                                slug=slug,
                                kind="want_shift",
                                detail=(
                                    f"wants already set (stated={sheet.wants.stated!r}, "
                                    f"real={sheet.wants.real!r}); proposed "
                                    f"stated={new_stated!r}, real={new_real!r}"
                                ),
                            )
                        )
                else:
                    sheet.wants.stated = new_stated or sheet.wants.stated
                    sheet.wants.real = new_real or sheet.wants.real
                    modified = True
                    result.applied.append({"slug": slug, "kind": "want_shift"})

        # -- new lies ------------------------------------------------------
        for nl in raw.get("new_lies") or []:
            if not isinstance(nl, dict):
                continue
            claim = str(nl.get("claim") or "").strip()
            if not claim:
                continue
            if any(_norm(lie.claim) == _norm(claim) for lie in sheet.lies):
                result.skipped.append({"slug": slug, "reason": "duplicate lie", "claim": claim})
                continue
            lie = Lie(
                id=store.next_lie_id(sheet),
                claim=claim,
                truth=str(nl.get("truth") or ""),
                audience=str(nl.get("audience") or "everyone"),
            )
            sheet.lies.append(lie)
            modified = True
            result.applied.append({"slug": slug, "kind": "lie", "id": lie.id, "claim": claim})

        # -- lie updates (exposure) ----------------------------------------
        for lu in raw.get("lie_updates") or []:
            if not isinstance(lu, dict):
                continue
            lid = str(lu.get("id") or "").strip()
            target = next((x for x in sheet.lies if x.id == lid), None)
            if target is None:
                result.skipped.append({"slug": slug, "reason": f"unknown lie id {lid!r}"})
                continue
            if not lu.get("exposed"):
                continue
            if target.exposed_in is not None:
                if target.exposed_in == chapter:
                    result.skipped.append(
                        {"slug": slug, "reason": f"lie {lid} already exposed this chapter"}
                    )
                else:
                    result.conflicts.append(
                        CastConflict(
                            slug=slug,
                            kind="lie_exposure",
                            detail=f"lie {lid} already exposed in ch-{target.exposed_in:02d}",
                            quote=str(lu.get("quote") or ""),
                        )
                    )
                continue
            target.exposed_in = chapter
            target.active = False
            modified = True
            result.applied.append({"slug": slug, "kind": "lie_exposed", "id": lid})

        # -- refusals (additive) -------------------------------------------
        for ru in raw.get("refusal_updates") or []:
            if not isinstance(ru, dict):
                continue
            topic = str(ru.get("topic") or "").strip()
            if not topic:
                continue
            if any(_norm(r.topic) == _norm(topic) for r in sheet.refusals):
                result.skipped.append({"slug": slug, "reason": "duplicate refusal", "topic": topic})
                continue
            sheet.refusals.append(Refusal(topic=topic, reason=str(ru.get("reason") or "")))
            modified = True
            result.applied.append({"slug": slug, "kind": "refusal", "topic": topic})

        if modified and auto:
            sheet.last_updated_chapter = max(sheet.last_updated_chapter, chapter)
            store.save(sheet)

    return result
