"""Knowledge boundedness: LLM attribution extraction feeding a pure ledger diff.

The model does the one thing regex cannot -- map "Ruth mentions the
foreclosure notice" to knowledge entry `k004`. The check itself is pure
arithmetic over the ledger: an entry referenced in chapter N whose
`learned_in > N` is a violation. Because the extraction step is LLM-assisted,
every finding here is advisory and never gates (invariant 2); the pure
`check_boundedness` is independently unit-testable with no provider.

Severity policy: a boundedness violation is major, escalating to critical when
the violated entry is a `secret` -- acting on an unlearned secret is the worst
class of leak. References to knowledge no ledger records surface as info-level
"unlogged knowledge" findings pointing at `stoner cast update`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..review.passes import locate_span
from ..types import Finding, Severity
from .sheet import CastSheet
from .store import review_digest  # noqa: F401  (re-exported for callers)

_SOURCE = "cast:boundedness"


class BoundednessError(RuntimeError):
    """Raised when attribution model output cannot be parsed."""


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

_SCHEMA_INSTRUCTIONS = """\
Respond with STRICT JSON only -- no prose outside a single ```json block or
raw JSON. The shape is exactly:

{
  "attributions": [
    {"character": "<slug>", "entry_id": "<k-id from that character's ledger, or null>",
     "quote": "<short verbatim quote from the chapter>", "basis": "speaks|acts|thinks"}
  ]
}

Rules:
- Produce one record for every place a character's dialogue, action, or
  interior narration relies on a specific ledger fact -- map it to that
  character's knowledge-entry id.
- If a character relies on knowledge NOT in their ledger, emit a record with
  `entry_id: null` so the gap is visible.
- `quote` must be copied verbatim so it can be located in the text.
- Only reference entry ids that appear in the ledger tables above.
"""


def attribution_prompt(chapter_text: str, sheets_digest: str) -> str:
    """Build the attribution user prompt: ledgers + chapter + JSON schema ask."""
    return (
        "You are checking knowledge boundedness for a novel. For the chapter "
        "below, attribute every place a character relies on a known fact to the "
        "matching knowledge-entry id from their ledger.\n\n"
        "## Cast Ledgers\n\n"
        f"{sheets_digest.strip() or '(no cast sheets)'}\n\n"
        "## Chapter Text\n\n"
        f"{chapter_text.strip()}\n\n"
        "## Task\n\n"
        f"{_SCHEMA_INSTRUCTIONS}"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_attributions(text: str) -> list[dict[str, Any]]:
    """Tolerantly extract the `attributions` list from raw model output."""
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
        if isinstance(parsed, dict) and isinstance(parsed.get("attributions"), list):
            return [a for a in parsed["attributions"] if isinstance(a, dict)]
    raise BoundednessError("could not find valid JSON attributions in output")


# ---------------------------------------------------------------------------
# The pure check
# ---------------------------------------------------------------------------


def check_boundedness(
    attributions: list[dict[str, Any]],
    sheets: list[CastSheet],
    chapter: int,
    body: str | None = None,
) -> list[Finding]:
    """Diff attributions against the ledgers -- a pure function, no provider.

    A resolved entry with `learned_in > chapter` is a violation (major;
    critical when the entry is a secret). A null entry_id is an info-level
    "unlogged knowledge" finding. An entry_id no ledger records, or a
    character with no sheet, becomes an info-level parse note rather than a
    crash. When `body` is given, quotes are located to spans.
    """
    by_slug = {s.slug: s for s in sheets}
    findings: list[Finding] = []

    for a in attributions:
        character = str(a.get("character") or "").strip()
        entry_id = a.get("entry_id")
        quote = str(a.get("quote") or "")

        sheet = by_slug.get(character)
        if sheet is None:
            findings.append(
                Finding(
                    source=_SOURCE,
                    severity=Severity.info,
                    category="unknown-character",
                    quote=quote,
                    issue=f"attribution references character {character!r} with no cast sheet",
                )
            )
            continue

        if entry_id in (None, "", "null"):
            findings.append(
                Finding(
                    source=_SOURCE,
                    severity=Severity.info,
                    category="unlogged-knowledge",
                    quote=quote,
                    issue=(
                        f"{sheet.name} relies on knowledge no ledger records "
                        f"(basis: {a.get('basis', '?')})"
                    ),
                    suggestion="log it with `stoner cast update`",
                )
            )
            continue

        entry = next((e for e in sheet.knowledge if e.id == str(entry_id)), None)
        if entry is None:
            findings.append(
                Finding(
                    source=_SOURCE,
                    severity=Severity.info,
                    category="unknown-entry",
                    quote=quote,
                    issue=f"attribution references entry {entry_id!r} not in {sheet.name}'s ledger",
                )
            )
            continue

        if entry.learned_in <= chapter:
            continue  # bounded -- the character has learned this

        severity = Severity.critical if entry.secret else Severity.major
        secret_note = " (a secret)" if entry.secret else ""
        findings.append(
            Finding(
                source=_SOURCE,
                severity=severity,
                category="anachronistic-knowledge",
                quote=quote,
                issue=(
                    f"{sheet.name} acts on {entry.id}{secret_note} in ch-{chapter:02d}, "
                    f"but learns it in ch-{entry.learned_in:02d}: {entry.fact!r}"
                ),
                suggestion="move the reveal earlier, or cut the reference",
            )
        )

    if body:
        for f in findings:
            if f.quote and f.span is None:
                f.span = locate_span(body, f.quote)
    return findings
