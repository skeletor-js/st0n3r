"""The write pipeline: plan -> draft -> slop gate -> archive.

Each stage is also reachable standalone through the CLI; this module wires
them into the opinionated flow described in ARCHITECTURE.md.

The single-shot draft path (text-only CLI providers, the ``not
supports_tools`` branch of :func:`draft_chapter`) runs one deterministic
sanitation pass, :func:`sanitize_single_shot_prose`, before saving: those
providers occasionally echo tool-call scaffolding verbatim into the
completion (plan 011 live-proof pollution), and the sanitizer strips only the
unambiguous machine artifacts while failing open so the 200-word refusal
guard stays the real gate.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any

from ..archaeology.snapshots import snapshot_write_chapter
from ..canon.archivist import (
    ApplyResult,
    apply_updates,
    extract_facts_prompt,
    parse_archivist_json,
)
from ..canon.memory import Memory
from ..canon.scaffold import new_chapter_stub
from ..canon.store import CanonStore
from ..engine.agent import Agent
from ..engine.tools import default_registry
from ..ledger import Ledger
from ..project import WritingProject, count_words
from ..providers.base import Provider
from ..providers.registry import get_provider
from ..slop import run_slop
from ..types import Usage
from .common import call_model, chapter_context, render_prompt, resolve_role_model


@dataclass
class WriteResult:
    chapter: int
    words: int = 0
    slop_before: float = -1.0
    slop_after: float = -1.0
    voice_before: float = -1.0
    voice_after: float = -1.0
    revision_loops: int = 0
    gate_passed: bool = False
    archive: ApplyResult | None = None
    usage: Usage = field(default_factory=Usage)
    notes: list[str] = field(default_factory=list)


def _slop_gate_fails(project: WritingProject, score: float, severities: set[str]) -> bool:
    gates = project.config.gates
    if score > gates.slop_max_score:
        return True
    return bool(severities & set(gates.slop_block_severities))


# --- single-shot sanitation ------------------------------------------------
# Each pattern matches ONE unambiguous machine artifact a text-only CLI
# provider can echo verbatim into a draft (plan 011 live-proof pollution).
# They are deliberately tight: prose that merely *mentions* an angle bracket
# or the word "Wrote" must survive untouched, and anything past these shapes
# is left for the word-count guard rather than guessed at.
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
# A conversational preamble line ("I'll write it and save via the tool.") but
# ONLY when a tool-call tag directly follows it. The line carries no '<', so a
# bare first line of prose (never followed by <invoke>) can never match.
_PREAMBLE_RE = re.compile(
    r"\A\s*[^\n<]{1,200}?\s*\n\s*\n?\s*(?=<(?:\w+:)?(?:invoke|parameter)\b)"
)
# An <invoke> wrapper carrying the prose in a body/content parameter. The
# closing </invoke> is optional so a stray "</parameter></invoke>" tail (or a
# truncated call) still unwraps; the '.*?' after the open tolerates a leading
# stray token or sibling parameter (e.g. name="number").
_INVOKE_BODY_RE = re.compile(
    r'<(?:\w+:)?invoke\b[^>]*>.*?'
    r'<(?:\w+:)?parameter\s+name="(?:body|content)"[^>]*>'
    r"(?P<body>.*?)"
    r"</(?:\w+:)?parameter>"
    r"\s*(?:</(?:\w+:)?invoke>)?",
    re.DOTALL,
)
# Residual tool-call tags (the no-body wrapper case, or a dangling tail left
# after unwrapping): drop the tag markup, keep any surrounding prose.
_TOOL_TAG_RE = re.compile(r"</?(?:\w+:)?(?:invoke|parameter)\b[^>]*>", re.DOTALL)
# A provider log line: "Wrote 8993 characters to /abs/path". Anchored to a
# whole line and an absolute path so a sentence starting "Wrote" is safe.
_WROTE_LINE_RE = re.compile(r"^Wrote \d+ characters to /.*$", re.MULTILINE)
# A trailing JSON-envelope fragment: literal '\n' escapes running into a
# closing '"}' at end of text. Real prose has real newlines, not backslash-n
# followed by a JSON close, so this only fires on serialized tool-call tails.
_JSON_TAIL_RE = re.compile(r'\s*(?:\\n)+[^\n]*"\}\s*\Z')


def sanitize_single_shot_prose(text: str) -> tuple[str, list[str]]:
    r"""Strip verbatim machine scaffolding from a single-shot draft.

    Text-only CLI providers (codex/claude) occasionally echo tool-call
    scaffolding into the completion instead of returning bare prose (plan 011
    live-proof pollution). This removes only UNAMBIGUOUS artifacts and fails
    open: if cleaning would drop the draft below half its original word count,
    the original text is returned untouched and the 200-word refusal guard in
    :func:`draft_chapter` stays the real gate.

    Returns ``(cleaned_text, notes)`` where ``notes`` names what was stripped
    (empty when the text was already clean; clean input is returned
    byte-identical). Handled shapes:

    - a complete ``<system-reminder>...</system-reminder>`` block;
    - an ``<invoke>`` tool-call wrapper: the ``<parameter name="body">`` /
      ``name="content"`` prose is unwrapped and the surrounding XML dropped
      (a no-body wrapper or stray ``</parameter></invoke>`` tail just has its
      tags removed);
    - a leading conversational preamble line, but ONLY when it directly
      precedes such a wrapper (a bare first line of prose is never touched);
    - standalone ``Wrote N characters to /abs/path`` provider log lines;
    - a trailing JSON-envelope fragment (``...\n\n..."}`` with literal ``\n``).
    """
    notes: list[str] = []
    original = text
    cleaned = text

    cleaned, n = _SYSTEM_REMINDER_RE.subn("", cleaned)
    if n:
        notes.append(f"stripped {n} <system-reminder> block(s)")

    # Judge the preamble before the wrapper is removed: the "immediately
    # followed by an XML wrapper" relationship only exists while the tags are
    # still present.
    cleaned, n = _PREAMBLE_RE.subn("", cleaned, count=1)
    if n:
        notes.append("stripped leading tool-call preamble line")

    cleaned, n = _INVOKE_BODY_RE.subn(lambda m: m.group("body"), cleaned)
    if n:
        notes.append(f"unwrapped {n} tool-call body wrapper(s)")

    cleaned, n = _TOOL_TAG_RE.subn("", cleaned)
    if n:
        notes.append(f"removed {n} stray tool-call XML tag(s)")

    cleaned, n = _JSON_TAIL_RE.subn("", cleaned)
    if n:
        notes.append("removed trailing JSON envelope fragment")

    cleaned, n = _WROTE_LINE_RE.subn("", cleaned)
    if n:
        notes.append(f"removed {n} provider log line(s)")

    if not notes:
        return original, []

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    if count_words(cleaned) < count_words(original) / 2:
        return original, [
            "single-shot sanitizer stood down: cleaning would have removed "
            "more than half the words; left the draft untouched"
        ]

    return cleaned, notes


def draft_chapter(
    project: WritingProject,
    number: int,
    model: str | None = None,
    provider: Provider | None = None,
    task: str = "",
) -> Usage:
    """Run the writer agent to draft one chapter (writes the chapter file)."""
    ctx = chapter_context(project, number)
    fm: dict[str, Any] = {}
    existing = ""
    try:
        fm, existing = project.read_chapter(number)
    except Exception:
        fm, _body = new_chapter_stub(number)
    ctx["chapter_title"] = str(fm.get("title", ""))

    system = render_prompt("writer.md", ctx)
    model_str = resolve_role_model(project.config, "writer", model)
    if provider is None:
        provider, model_id = get_provider(model_str, project.config)
    else:
        model_id = model_str.split("/", 1)[1] if "/" in model_str else model_str

    ledger = Ledger(project.root)

    if not provider.supports_tools:
        # Text-only backends (codex/claude CLI) draft in ONE comprehensive
        # completion instead of the tool loop: every loop turn re-sends the
        # whole conversation through the CLI and invites protocol drift,
        # while the system prompt already carries canon, beats, and memory.
        from ..types import CompletionRequest, Message

        user = task or (
            f"Write chapter {number} now, in full, as finished prose. "
            "Everything you need — premise, style, canon, beat sheet, memory, "
            "the tail of the previous chapter — is in your instructions above. "
            "Reply with ONLY the chapter prose: no title line, no notes, no "
            "commentary before or after."
        )
        resp = provider.complete(
            CompletionRequest(
                model=model_id,
                system=system,
                messages=[Message(role="user", content=user)],
                max_tokens=project.config.max_tokens,
                temperature=project.config.temperature,
            )
        )
        body = resp.text.strip()
        # Defensive: strip any tool-call scaffolding the text-only CLI echoed
        # into the completion before the word-count guard weighs the prose.
        body, sanitize_notes = sanitize_single_shot_prose(body)
        if fm.get("status") in (None, "", "outline"):
            fm["status"] = "draft"
        if count_words(body) <= 200:
            raise RuntimeError(
                f"Single-shot draft for chapter {number} came back with only "
                f"{count_words(body)} words; not saving. Raise max_tokens or retry."
            )
        snapshot_write_chapter(project, number, fm, body, reason="draft")
        if sanitize_notes:
            ledger.append(
                "write.single_shot",
                target=project.chapter_rel(number),
                sanitized=sanitize_notes,
            )
        else:
            ledger.append("write.single_shot", target=project.chapter_rel(number))
        return resp.usage
    agent = Agent(
        provider=provider,
        model_id=model_id,
        tools=default_registry(),
        project=project,
        ledger=ledger,
        session_name=f"write-ch{number:02d}",
    )
    default_task = (
        f"Draft chapter {number} now. Consult the beat sheet and canon as "
        f"needed, then save the finished prose with the write_chapter tool "
        f"(number={number}). The chapter should be complete, publishable "
        f"prose — not an outline or summary. If tool calling fails "
        f"repeatedly, reply with ONLY the complete chapter prose as your "
        f"final message and it will be saved for you."
    )
    result = agent.run(task=task or default_task, system=system)

    # Fallback: agent produced prose but never called write_chapter.
    try:
        project.read_chapter(number)
    except Exception:
        if count_words(result.text) > 200:
            snapshot_write_chapter(project, number, fm, result.text, reason="draft")
            ledger.append("write.fallback_save", target=project.chapter_rel(number))
        else:
            raise RuntimeError(
                f"Writer agent finished without producing chapter {number} "
                f"(final message was {count_words(result.text)} words). "
                "See the session transcript in .stoner/sessions/."
            ) from None
    return result.usage


def _draft_via_tournament(
    project: WritingProject,
    number: int,
    takes: int,
    model: str | None = None,
    provider: Provider | None = None,
) -> Usage:
    """Draft `takes` angled takes, judge them blind, and apply the proposed
    winner to the manuscript — the `stoner write --tournament N` / book-slot
    drafting path (plan 003 seam, plan 011 U4).

    Runs before the write pipeline's slop gate: the applied winner lands as a
    `draft`-status chapter (snapshot reason `tournament-graft`; the per-take
    drafts carry reason `draft`), and the caller then runs the normal slop
    gate/archivist flow over it. `run_tournament`/`apply_winner` ledger their
    own `tournament.*` actions ahead of the pipeline's `pipeline.write.*`.
    """
    from ..tournament.run import apply_winner, run_tournament

    usage = Usage()
    tour = run_tournament(project, number, takes=takes, model=model, provider=provider)
    usage += tour.usage
    if tour.proposed_winner is None:
        raise RuntimeError(
            f"tournament for chapter {number} produced no winner "
            f"(status {tour.status!r}); cannot continue the write pipeline"
        )
    # `--tournament` on write IS the human-confirmation step: the explicit
    # flag is consent to apply the proposed winner (normally a separate
    # `stoner tournament apply`).
    applied = apply_winner(project, tour.id, model=model, provider=provider)
    usage += applied.usage
    return usage


def run_write(
    project: WritingProject,
    number: int,
    model: str | None = None,
    provider: Provider | None = None,
    skip_archive: bool = False,
    task: str = "",
    tournament: int | None = None,
) -> WriteResult:
    """Full pipeline: draft -> slop gate (auto-revise) -> voice gate -> archivist.

    `model` overrides the *writer* role only; the revise and archivist
    stages keep their configured role models (unless a `provider` instance
    is injected, which routes every stage — that path exists for tests).

    `tournament=N` (opt-in) drafts N angled takes, judges them, and applies
    the winner before the slop gate instead of a single draft (plan 011 U4).
    """
    res = WriteResult(chapter=number)
    ledger = Ledger(project.root)

    # Optional tournament drafting runs first so its `tournament.*` ledger
    # entries precede `pipeline.write.*` (plan 011 U4).
    skip_draft = False
    if tournament is not None:
        res.usage += _draft_via_tournament(
            project, number, takes=tournament, model=model, provider=provider
        )
        skip_draft = True

    if provider is None:
        # Auth-preflight the writer provider before logging the pipeline
        # start, so a missing API key doesn't leave an orphaned start entry
        # with no matching done. Each stage still resolves its own role.
        get_provider(resolve_role_model(project.config, "writer", model), project.config)
    ledger.append("pipeline.write.start", target=project.chapter_rel(number))

    if not skip_draft:
        res.usage += draft_chapter(project, number, model=model, provider=provider, task=task)

    # --- slop gate -----------------------------------------------------
    bw, bp = CanonStore(project).banned_terms()
    _, body = project.read_chapter(number)
    report = run_slop(body, path=project.chapter_rel(number), banned_words=bw, banned_phrases=bp)
    res.slop_before = res.slop_after = report.score
    gates = project.config.gates

    while res.revision_loops < gates.max_revision_loops and _slop_gate_fails(
        project, report.score, {f.severity.value for f in report.findings}
    ):
        res.revision_loops += 1
        ledger.append(
            "pipeline.write.slop_revise",
            target=project.chapter_rel(number),
            score=report.score,
            loop=res.revision_loops,
        )
        from ..review.revise import revise_chapter  # late import; parallel module

        blocked = [
            f
            for f in report.findings
            if f.severity.value in ("major", "critical") or report.score > gates.slop_max_score
        ][:40]
        # model deliberately not forwarded: the revise stage keeps its
        # configured reviewer role even when the writer model is overridden.
        # `reason` tags the draft snapshot; passed only when the (replaceable,
        # late-imported) callable accepts it.
        revise_kwargs: dict[str, Any] = {}
        if "reason" in inspect.signature(revise_chapter).parameters:
            revise_kwargs["reason"] = "slop-revise"
        revision = revise_chapter(
            project, number, blocked or report.findings[:40], provider=provider, **revise_kwargs
        )
        res.usage += revision.usage
        _, body = project.read_chapter(number)
        report = run_slop(body, path=project.chapter_rel(number), banned_words=bw, banned_phrases=bp)
        res.slop_after = report.score

    res.gate_passed = not _slop_gate_fails(
        project, report.score, {f.severity.value for f in report.findings}
    )
    if not res.gate_passed:
        res.notes.append(
            f"slop gate still failing after {res.revision_loops} loop(s): "
            f"score {report.score:.1f} (max {gates.slop_max_score})"
        )

    # --- voice gate (opt-in; default-off) --------------------------------
    # Deterministic drift check settles after the slop gate. `voice_gate_check`
    # returns (None, False) when `voice.gate` is off or no fingerprint exists,
    # so the default path adds no `voice.*` ledger entries and is byte-identical
    # to v0.2.0. Revision loops continue from the slop loop's count against the
    # shared `gates.max_revision_loops` budget (plan 001 U5).
    from ..voice.drift import voice_gate_check

    _, body = project.read_chapter(number)
    voice_report, voice_fails = voice_gate_check(
        project, body, path=project.chapter_rel(number)
    )
    if voice_report is None:
        # The helper collapses "gate off" and "gate on but no fingerprint" to
        # None. Only the second case warrants a note: the writer asked for a
        # voice gate but nothing was there to check against.
        if project.config.voice.gate:
            res.notes.append(
                "voice gate enabled but no fingerprint learned "
                "(run `stoner voice learn`); skipping voice check"
            )
    else:
        res.voice_before = res.voice_after = voice_report.score
        while res.revision_loops < gates.max_revision_loops and voice_fails:
            voice_findings = [
                f for f in voice_report.findings if f.severity.value in ("major", "critical")
            ][:40] or voice_report.findings[:40]
            if not voice_findings:
                break  # nothing actionable to revise against
            res.revision_loops += 1
            ledger.append(
                "voice.gate",
                target=project.chapter_rel(number),
                score=voice_report.score,
                loop=res.revision_loops,
            )
            from ..review.revise import revise_chapter

            voice_revise_kwargs: dict[str, Any] = {}
            if "reason" in inspect.signature(revise_chapter).parameters:
                voice_revise_kwargs["reason"] = "voice-revise"
            revision = revise_chapter(
                project, number, voice_findings, provider=provider, **voice_revise_kwargs
            )
            res.usage += revision.usage
            _, body = project.read_chapter(number)
            voice_report, voice_fails = voice_gate_check(
                project, body, path=project.chapter_rel(number)
            )
            if voice_report is None:
                break  # gate turned off mid-run (defensive); nothing more to do
            res.voice_after = voice_report.score
        if voice_fails:
            res.notes.append(
                f"voice gate still failing after {res.revision_loops} loop(s): "
                f"score {res.voice_after:.1f} (max {project.config.voice.max_drift_score})"
            )

    # --- archivist -------------------------------------------------------
    if not skip_archive:
        res.archive = run_archive(project, number, provider=provider, auto=True)
        _run_cast_auto_update(project, number, provider=provider, result=res)

    fm, body = project.read_chapter(number)
    res.words = count_words(body)
    ledger.append(
        "pipeline.write.done",
        target=project.chapter_rel(number),
        words=res.words,
        slop=res.slop_after,
        gate_passed=res.gate_passed,
    )
    return res


def _run_cast_auto_update(
    project: WritingProject,
    number: int,
    provider: Provider | None,
    result: WriteResult,
) -> None:
    """Post-archivist cast-curator hook (plan 002 U7, plan 011 U4).

    Fires only when `config.cast.auto_update` is true AND cast sheets exist —
    a project that never opts into cast pays nothing (no model call, no
    `cast.*` ledger). Private-state conflicts add one summary line to
    `result.notes`; any curator failure degrades to a note and never fails
    the write.
    """
    if not project.config.cast.auto_update:
        return
    try:
        from ..interiority.pipeline import run_cast_update
        from ..interiority.store import CastStore

        if not CastStore(project).list_slugs():
            return
        cast_res = run_cast_update(project, number, auto=True, provider=provider)
        if cast_res.conflicts:
            result.notes.append(
                f"cast: {len(cast_res.conflicts)} private-state conflict(s) need review"
            )
    except Exception as e:  # noqa: BLE001 - cast curator must never fail the write
        result.notes.append(f"cast auto-update failed: {e}")


def run_archive(
    project: WritingProject,
    number: int,
    model: str | None = None,
    provider: Provider | None = None,
    auto: bool = False,
) -> ApplyResult:
    """Archivist stage: extract facts from a chapter, diff canon, apply."""
    store = CanonStore(project)
    memory = Memory(project)
    _, body = project.read_chapter(number)

    ctx = chapter_context(project, number)
    system = render_prompt("archivist.md", {**ctx, "chapter_text": ""})
    user = extract_facts_prompt(body, store.context_pack(max_chars=8000))
    text, _usage = call_model(
        project, "archivist", system=system, user=user, model=model, provider=provider
    )
    parsed = parse_archivist_json(text)
    result = apply_updates(store, memory, parsed, number, auto=auto)
    if auto:
        memory.rebuild_book_so_far()
    Ledger(project.root).append(
        "canon.archive",
        target=project.chapter_rel(number),
        applied=len(result.applied_facts),
        conflicts=len(result.conflicts),
        auto=auto,
    )
    return result
