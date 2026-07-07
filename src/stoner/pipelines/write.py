"""The write pipeline: plan -> draft -> slop gate -> archive.

Each stage is also reachable standalone through the CLI; this module wires
them into the opinionated flow described in ARCHITECTURE.md.
"""

from __future__ import annotations

import inspect
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


def draft_chapter(
    project: WritingProject,
    number: int,
    model: str | None = None,
    provider: Provider | None = None,
    task: str = "",
) -> Usage:
    """Run the writer agent to draft one chapter (writes the chapter file)."""
    ctx = chapter_context(project, number)
    fm, existing = ({}, "")
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
        if fm.get("status") in (None, "", "outline"):
            fm["status"] = "draft"
        if count_words(body) <= 200:
            raise RuntimeError(
                f"Single-shot draft for chapter {number} came back with only "
                f"{count_words(body)} words; not saving. Raise max_tokens or retry."
            )
        snapshot_write_chapter(project, number, fm, body, reason="draft")
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


def run_write(
    project: WritingProject,
    number: int,
    model: str | None = None,
    provider: Provider | None = None,
    skip_archive: bool = False,
    task: str = "",
) -> WriteResult:
    """Full pipeline: draft -> slop gate (auto-revise) -> archivist.

    `model` overrides the *writer* role only; the revise and archivist
    stages keep their configured role models (unless a `provider` instance
    is injected, which routes every stage — that path exists for tests).
    """
    res = WriteResult(chapter=number)
    ledger = Ledger(project.root)
    if provider is None:
        # Auth-preflight the writer provider before logging the pipeline
        # start, so a missing API key doesn't leave an orphaned start entry
        # with no matching done. Each stage still resolves its own role.
        get_provider(resolve_role_model(project.config, "writer", model), project.config)
    ledger.append("pipeline.write.start", target=project.chapter_rel(number))

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

    # --- archivist -------------------------------------------------------
    if not skip_archive:
        res.archive = run_archive(project, number, provider=provider, auto=True)

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
