"""Built-in critic passes: prompt assembly + tolerant JSON parsing.

Each `ReviewPass` is a pure prompt-builder/parser pair -- like
`canon.archivist`, this module never calls a provider itself; `runner.py`
owns the model call and the sequencing across passes (see
`docs/planning/ARCHITECTURE.md` "Review engine" and
`docs/research/RESEARCH.md` "Design consequences": adversarial cut lists,
multi-persona panels, and comparative STRONG/FINE/WEAK/CUT grading instead
of absolute 1-10 scores, because absolute LLM scoring collapses into a
narrow band while comparative judgments spread out).

All passes ask the model for STRICT JSON: `{"findings": [...], "summary":
"..."}`, optionally extended with pass-specific keys (`panel` adds
`personas`/`consensus`, `grade` adds `grades`). `extract_json` is the one
tolerant extractor every pass (and the runner) shares.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..canon.memory import Memory
from ..canon.store import CanonStore
from ..project import ProjectError, WritingProject
from ..types import Finding, Severity, Span
from ..voice.drift import voice_context_digest

_MAX_FINDINGS_PER_PASS = 25
_CANON_DIGEST_CHARS = 8000
_PRIOR_TAIL_CHARS = 1500

# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


@dataclass
class PassContext:
    """Everything a pass's `build_prompt` needs, assembled once per review run."""

    project: WritingProject
    chapter: int
    chapter_body: str
    canon_digest: str
    memory_context: str
    style_excerpt: str
    prior_tail: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    voice_digest: str = ""  # measured fingerprint digest; "" when none learned


def build_context(project: WritingProject, chapter: int) -> PassContext:
    """Assemble the shared context pack: chapter body, canon digest (capped
    ~8k chars), rolling memory, style excerpt, and the tail of the prior
    chapter (if any) -- mirroring what the writer agent sees, per
    `docs/planning/ARCHITECTURE.md` ("the agent never reads whole
    manuscripts into context")."""
    fm, body = project.read_chapter(chapter)
    store = CanonStore(project)
    canon_digest = store.context_pack(max_chars=_CANON_DIGEST_CHARS)
    memory_context = Memory(project).context_for_chapter(chapter)
    style_excerpt = store.style_body_without_banned()
    prior_tail = ""
    if chapter > 1:
        try:
            _, prior_body = project.read_chapter(chapter - 1)
            prior_tail = prior_body[-_PRIOR_TAIL_CHARS:]
        except ProjectError:
            prior_tail = ""
    return PassContext(
        project=project,
        chapter=chapter,
        chapter_body=body,
        canon_digest=canon_digest,
        memory_context=memory_context,
        style_excerpt=style_excerpt,
        prior_tail=prior_tail,
        frontmatter=fm,
        # "" when no fingerprint has been learned -- passes behave as before.
        voice_digest=voice_context_digest(project, body),
    )


# ---------------------------------------------------------------------------
# Shared tolerant JSON extraction + span location
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> dict[str, Any]:
    """Tolerantly pull a JSON object out of raw model output.

    Tries, in order: the whole trimmed string, each fenced ``` block (with
    or without a `json` tag), then the first `{...}` span found via
    brace-matching on position (handles trailing prose after valid JSON).
    Returns `{}` if nothing parses -- callers treat that as "unparseable".
    """
    if not text or not text.strip():
        return {}
    candidates: list[str] = [text.strip()]
    candidates.extend(m.strip() for m in _FENCE_RE.findall(text))
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def locate_span(body: str, quote: str) -> Span | None:
    """Find `quote` verbatim in `body` and build a Span for its first
    occurrence, or None if it isn't found (findings default to span=None)."""
    if not quote:
        return None
    idx = body.find(quote)
    if idx == -1:
        return None
    line = body.count("\n", 0, idx) + 1
    return Span(start=idx, end=idx + len(quote), line=line)


def _coerce_severity(value: Any) -> Severity:
    try:
        return Severity(str(value).strip().lower())
    except ValueError:
        return Severity.minor


def _findings_from_list(items: Any, source: str) -> list[Finding]:
    out: list[Finding] = []
    if not isinstance(items, list):
        return out
    for item in items[:_MAX_FINDINGS_PER_PASS]:
        if not isinstance(item, dict):
            continue
        out.append(
            Finding(
                source=source,
                severity=_coerce_severity(item.get("severity", "minor")),
                category=str(item.get("category", "")),
                quote=str(item.get("quote", "")),
                issue=str(item.get("issue", "")),
                suggestion=str(item.get("suggestion", "")),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Prompt building helpers
# ---------------------------------------------------------------------------

def _shape(example: dict[str, Any]) -> str:
    """Render an example response shape as compact JSON for a prompt."""
    return json.dumps(example)


_JSON_INSTRUCTIONS = (
    "Respond with STRICT JSON only (a single ```json fenced block or raw "
    "JSON, no other prose) matching exactly this shape:\n\n"
    + _shape(
        {
            "findings": [
                {
                    "severity": "info|minor|major|critical",
                    "category": "<short bucket>",
                    "quote": "<verbatim short quote from the chapter, or empty string if none applies>",
                    "issue": "<what is wrong>",
                    "suggestion": "<how to fix it>",
                }
            ],
            "summary": "<one-paragraph overview>",
        }
    )
    + "\n\n"
    "Quotes must be copied verbatim from the chapter text so they can be "
    f"located programmatically. List at most {_MAX_FINDINGS_PER_PASS} "
    "findings, most important first. If there is nothing to flag, return an "
    "empty findings list -- do not invent issues."
)


def _std_user_prompt(ctx: PassContext, task: str, json_instructions: str = _JSON_INSTRUCTIONS) -> str:
    parts = [f"## Chapter {ctx.chapter} (full text)\n\n{ctx.chapter_body}"]
    if ctx.canon_digest:
        parts.append(f"## Canon\n\n{ctx.canon_digest}")
    if ctx.memory_context:
        parts.append(f"## Memory / Story So Far\n\n{ctx.memory_context}")
    if ctx.style_excerpt:
        parts.append(f"## Style Guide\n\n{ctx.style_excerpt}")
    if ctx.prior_tail:
        parts.append(f"## Tail of Previous Chapter\n\n{ctx.prior_tail}")
    parts.append(f"## Task\n\n{task}")
    parts.append(json_instructions)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# ReviewPass definition + registry
# ---------------------------------------------------------------------------


@dataclass
class ReviewPass:
    """A critic pass: a prompt builder plus a tolerant response parser."""

    name: str
    description: str
    build_prompt: Callable[[PassContext], tuple[str, str]]
    parse: Callable[[str], list[Finding]]


def _make_standard_parser(name: str) -> Callable[[str], list[Finding]]:
    source = f"review:{name}"

    def parse(text: str) -> list[Finding]:
        return _findings_from_list(extract_json(text).get("findings"), source)

    return parse


# -- continuity ---------------------------------------------------------

_CONTINUITY_SYSTEM = (
    "You are a meticulous continuity editor for a novel-in-progress. You "
    "cross-check chapters against the project's canon (facts, timeline, "
    "character state) and its rolling memory of prior chapters."
)


def _continuity_prompt(ctx: PassContext) -> tuple[str, str]:
    task = (
        "Find continuity errors in this chapter: facts that contradict "
        "canon, timeline inconsistencies, or characters knowing/doing/being "
        "something inconsistent with their established state."
    )
    return _CONTINUITY_SYSTEM, _std_user_prompt(ctx, task)


# -- pacing ---------------------------------------------------------------

_PACING_SYSTEM = (
    "You are a developmental editor focused on pacing: the balance of "
    "scene versus summary, momentum, and rhythm across a chapter."
)


def _pacing_prompt(ctx: PassContext) -> tuple[str, str]:
    task = (
        "Assess pacing in this chapter: scene-vs-summary balance (most of a "
        "chapter should be in-scene, not summarized), places momentum stalls "
        "or rushes, and repeated chapter-ending structures."
    )
    return _PACING_SYSTEM, _std_user_prompt(ctx, task)


# -- voice ------------------------------------------------------------------

_VOICE_SYSTEM = (
    "You are a voice editor checking adherence to the book's style guide "
    "and whether characters remain distinct from one another."
)


def _voice_prompt(ctx: PassContext) -> tuple[str, str]:
    task = (
        "Check voice: adherence to the style guide above, character-voice "
        "distinctiveness, dialogue that relies on balanced-antithesis "
        "constructions ('not X, but Y'), and 'everyone sounds the same' "
        "problems."
    )
    if ctx.voice_digest:
        task += (
            "\n\n## Measured voice fingerprint\n\n"
            + ctx.voice_digest
            + "\n\nThe fingerprint above is a deterministic measurement of this "
            "writer's voice. Use it to explain and localize where the chapter "
            "drifts from the measured voice -- do not re-score or re-compute "
            "the numbers, and do not treat them as a gate."
        )
    return _VOICE_SYSTEM, _std_user_prompt(ctx, task)


# -- line ---------------------------------------------------------------

_LINE_SYSTEM = (
    "You are a line editor hunting for prose-level AI-writing tells at the "
    "sentence level."
)


def _line_prompt(ctx: PassContext) -> tuple[str, str]:
    task = (
        "Line-edit for: over-explaining (narration restating what the scene "
        "already showed), triadic listing ('X. Y. Z.' constructions), "
        "simile crutches, filter words (felt/saw/heard/realized/noticed), "
        "and dialogue with no stumbles or interruptions."
    )
    return _LINE_SYSTEM, _std_user_prompt(ctx, task)


# -- logic ------------------------------------------------------------------

_LOGIC_SYSTEM = "You are a story editor checking plot logic and causality."


def _logic_prompt(ctx: PassContext) -> tuple[str, str]:
    task = (
        "Check plot logic: holes, turns that aren't earned by what came "
        "before, and character motivation gaps."
    )
    return _LOGIC_SYSTEM, _std_user_prompt(ctx, task)


# -- adversarial --------------------------------------------------------

_ADVERSARIAL_SYSTEM = (
    "You are a ruthless developmental editor running a forced-cut exercise. "
    "Absolute praise is worthless; your job is to find exactly what does "
    "not need to exist."
)

_CUT_CATEGORIES = {"OVER-EXPLAIN", "REDUNDANT", "THROAT-CLEARING", "WEAK-BEAT", "OTHER"}

_JSON_INSTRUCTIONS_ADVERSARIAL = (
    "Respond with STRICT JSON only matching exactly this shape:\n\n"
    + _shape(
        {
            "findings": [
                {
                    "severity": "minor|major",
                    "category": "OVER-EXPLAIN|REDUNDANT|THROAT-CLEARING|WEAK-BEAT|OTHER",
                    "quote": "<verbatim text you would cut>",
                    "issue": "<why this can go>",
                    "suggestion": "<the cut instruction -- what to remove or how to tighten it>",
                }
            ],
            "summary": "<total words you would cut and the overall rationale>",
        }
    )
    + "\n\n"
    f"List at most {_MAX_FINDINGS_PER_PASS} cuts, largest/most confident "
    "first. Quotes must be copied verbatim so they can be located."
)


def _adversarial_prompt(ctx: PassContext) -> tuple[str, str]:
    task = (
        "You must cut exactly 400 words from this chapter without losing "
        "the story it tells. List precisely what you would cut and why, "
        "classifying every cut."
    )
    return _ADVERSARIAL_SYSTEM, _std_user_prompt(ctx, task, _JSON_INSTRUCTIONS_ADVERSARIAL)


def _parse_adversarial(text: str) -> list[Finding]:
    data = extract_json(text)
    source = "review:adversarial"
    out: list[Finding] = []
    items = data.get("findings")
    if not isinstance(items, list):
        return out
    for item in items[:_MAX_FINDINGS_PER_PASS]:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip().upper() or "OTHER"
        if category not in _CUT_CATEGORIES:
            category = "OTHER"
        suggestion = str(item.get("suggestion") or item.get("issue") or "cut this passage")
        out.append(
            Finding(
                source=source,
                severity=_coerce_severity(item.get("severity", "minor")),
                category=category,
                quote=str(item.get("quote", "")),
                issue=str(item.get("issue", "")),
                suggestion=suggestion,
            )
        )
    return out


# -- panel --------------------------------------------------------------

_PANEL_SYSTEM = (
    "You are convening a four-person editorial panel in a single pass: an "
    "acquisitions editor judging marketability, a devoted genre reader "
    "judging whether the chapter delivers on genre promises, a rival "
    "novelist judging craft with blunt professional envy, and a "
    "first-time reader judging clarity with zero prior context."
)

_JSON_INSTRUCTIONS_PANEL = (
    "Respond with STRICT JSON only matching exactly this shape:\n\n"
    + _shape(
        {
            "personas": {
                "acquisitions_editor": "<notes>",
                "genre_reader": "<notes>",
                "rival_novelist": "<notes>",
                "first_time_reader": "<notes>",
            },
            "consensus": [
                {
                    "issue": "<the shared concern>",
                    "quote": "<verbatim supporting quote, or empty string>",
                    "suggestion": "<fix>",
                    "votes": "<number, 1-4, of personas who independently raised it>",
                }
            ],
            "summary": "<overall panel verdict>",
        }
    )
    + "\n\n"
    f"`votes` must be an integer 1-4, not a string. List at most "
    f"{_MAX_FINDINGS_PER_PASS} consensus items."
)


def _panel_prompt(ctx: PassContext) -> tuple[str, str]:
    task = (
        "Read the chapter once per persona above, then reconcile their "
        "notes into consensus items -- issues raised independently by 3 or "
        "more of the 4 personas."
    )
    return _PANEL_SYSTEM, _std_user_prompt(ctx, task, _JSON_INSTRUCTIONS_PANEL)


def _parse_panel(text: str) -> list[Finding]:
    data = extract_json(text)
    source = "review:panel"
    out: list[Finding] = []
    items = data.get("consensus")
    if not isinstance(items, list):
        return out
    for item in items[:_MAX_FINDINGS_PER_PASS]:
        if not isinstance(item, dict):
            continue
        try:
            votes = int(item.get("votes", 0) or 0)
        except (TypeError, ValueError):
            votes = 0
        is_consensus = votes >= 3
        out.append(
            Finding(
                source=source,
                severity=Severity.major if is_consensus else Severity.info,
                category="consensus" if is_consensus else "minority",
                quote=str(item.get("quote", "")),
                issue=str(item.get("issue", "")),
                suggestion=str(item.get("suggestion", "")),
            )
        )
    return out


# -- grade ------------------------------------------------------------------

_GRADE_SYSTEM = (
    "You are grading this chapter paragraph by paragraph using comparative "
    "labels rather than absolute scores -- absolute 1-10 scoring collapses "
    "into a narrow band and is not useful for editorial decisions."
)

_GRADE_LABELS = ("STRONG", "FINE", "WEAK", "CUT")
_GRADE_SEVERITY: dict[str, Severity] = {"WEAK": Severity.minor, "CUT": Severity.major}

_JSON_INSTRUCTIONS_GRADE = (
    "Respond with STRICT JSON only matching exactly this shape:\n\n"
    + _shape(
        {
            "grades": [
                {
                    "paragraph": "<1-based index>",
                    "quote": "<first few words of the paragraph, verbatim>",
                    "label": "STRONG|FINE|WEAK|CUT",
                    "reason": "<one line>",
                }
            ],
            "summary": "<overall verdict>",
        }
    )
    + "\n\n"
    "Grade every paragraph in order. Use only the four labels above -- "
    "never a numeric score."
)


def _grade_prompt(ctx: PassContext) -> tuple[str, str]:
    task = (
        "Split the chapter into paragraphs in reading order and label each "
        "one STRONG, FINE, WEAK, or CUT with a one-line reason."
    )
    return _GRADE_SYSTEM, _std_user_prompt(ctx, task, _JSON_INSTRUCTIONS_GRADE)


def _parse_grade(text: str) -> list[Finding]:
    data = extract_json(text)
    source = "review:grade"
    grades = data.get("grades")
    out: list[Finding] = []
    if not isinstance(grades, list):
        return out
    counts: Counter[str] = Counter()
    for item in grades:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip().upper()
        if label not in _GRADE_LABELS:
            continue
        counts[label] += 1
        severity = _GRADE_SEVERITY.get(label)
        if severity is None:
            continue
        out.append(
            Finding(
                source=source,
                severity=severity,
                category=label,
                quote=str(item.get("quote", "")),
                issue=str(item.get("reason", "")),
                suggestion="cut this paragraph" if label == "CUT" else "tighten this paragraph",
            )
        )
        if len(out) >= _MAX_FINDINGS_PER_PASS:
            break
    if counts:
        dist = ", ".join(f"{label}: {counts.get(label, 0)}" for label in _GRADE_LABELS)
        out.append(
            Finding(
                source=source,
                severity=Severity.info,
                category="distribution",
                issue=f"Paragraph grade distribution -- {dist}",
            )
        )
    return out[:_MAX_FINDINGS_PER_PASS]


# -- interiority --------------------------------------------------------

_INTERIORITY_SYSTEM = (
    "You are an interiority editor. You check a chapter against the cast's "
    "private state -- what each character knows, wants, fears, lies about, and "
    "refuses to say -- to protect subtext, dramatic irony, and information "
    "asymmetry. This is the one editor allowed to see private cast sheets."
)


def _interiority_prompt(ctx: PassContext) -> tuple[str, str]:
    # Import the digest helper lazily so the review module carries no
    # load-time dependency on the interiority feature (it is additive and
    # opt-in): the pass simply renders no sheets when the feature is unused.
    from ..interiority import review_digest

    digest = review_digest(ctx.project, ctx.chapter)
    if digest:
        task = (
            "Check this chapter against the cast's private state below. Flag: "
            "refusals violated without an on-page cause; active lies "
            "contradicted with no exposure beat; stated-vs-real want collapses "
            "(a character baldly narrating their real want); and missed "
            "dramatic-irony setups (reader-known secrets a scene simply "
            "ignores). Do not flag a character acting on knowledge they hold as "
            "of this chapter -- that is correct.\n\n"
            "## Cast private state (PRIVATE)\n\n" + digest
        )
    else:
        task = (
            "No cast sheets exist for this project, so there is no private "
            "state to check the chapter against. Return an empty findings list."
        )
    return _INTERIORITY_SYSTEM, _std_user_prompt(ctx, task)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PASSES: dict[str, ReviewPass] = {
    "continuity": ReviewPass(
        name="continuity",
        description="Facts/timeline/character-state contradictions vs. canon and memory.",
        build_prompt=_continuity_prompt,
        parse=_make_standard_parser("continuity"),
    ),
    "pacing": ReviewPass(
        name="pacing",
        description="Scene vs. summary balance, momentum loss, chapter-ending repetition.",
        build_prompt=_pacing_prompt,
        parse=_make_standard_parser("pacing"),
    ),
    "voice": ReviewPass(
        name="voice",
        description="Adherence to style.md and character-voice distinctiveness.",
        build_prompt=_voice_prompt,
        parse=_make_standard_parser("voice"),
    ),
    "line": ReviewPass(
        name="line",
        description="Prose-level tells: over-explain, triadic listing, simile crutch, filter words.",
        build_prompt=_line_prompt,
        parse=_make_standard_parser("line"),
    ),
    "logic": ReviewPass(
        name="logic",
        description="Plot holes, unearned turns, motivation gaps.",
        build_prompt=_logic_prompt,
        parse=_make_standard_parser("logic"),
    ),
    "adversarial": ReviewPass(
        name="adversarial",
        description="Forced 400-word cut exercise; every cut classified by reason.",
        build_prompt=_adversarial_prompt,
        parse=_parse_adversarial,
    ),
    "panel": ReviewPass(
        name="panel",
        description="Four-persona reader panel in one prompt; only 3/4-consensus items become findings.",
        build_prompt=_panel_prompt,
        parse=_parse_panel,
    ),
    "grade": ReviewPass(
        name="grade",
        description="Paragraph-level STRONG/FINE/WEAK/CUT comparative grading with distribution stats.",
        build_prompt=_grade_prompt,
        parse=_parse_grade,
    ),
    "interiority": ReviewPass(
        name="interiority",
        description="Cast private state vs. the draft: broken refusals, dropped lies, want collapses, missed irony (advisory; opt-in).",
        build_prompt=_interiority_prompt,
        parse=_make_standard_parser("interiority"),
    ),
}
