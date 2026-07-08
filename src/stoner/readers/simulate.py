"""The core run loop: batched persona reads with rolling state and budget.

`run_readers` simulates a roster reading chapters in manuscript order. Each
chapter is read by the roster in batches of `personas_per_call` (the cost
lever): one plain, no-tools completion covers one chapter for k personas, so
text-only providers work natively (invariant 9). Per call the context is one
chapter body + a capped canon slice + the k batched personas' compact states --
never the whole manuscript (invariant 7).

State is saved BEFORE every model call (invariant 10). A chapter's rolled reader
state and its log are held locally and committed to the run state only after all
of that chapter's batches succeed, so a crash or budget stop mid-chapter never
double-rolls a reader on resume -- the chapter simply re-reads cleanly. A failed
or unparseable batch degrades to a recorded miss for the affected personas and
the run continues (invariant 8, the review runner's never-abort posture). The
run stops cleanly at `max_calls_per_run` and `--resume` continues it.

`batch_completion` is the reusable batch-call helper the bench pipeline (U5)
shares.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..canon.store import CanonStore
from ..ledger import Ledger
from ..pipelines.common import call_model
from ..pipelines.common import render_prompt as _render_prompt
from ..project import ProjectError, WritingProject
from ..providers.base import Provider
from ..review.passes import extract_json, locate_span
from ..types import Usage
from .personas import Persona, load_personas, select_roster
from .state import (
    ChapterLog,
    Marker,
    ReaderState,
    RunKind,
    RunState,
    list_runs,
    load_state,
    new_run_id,
    save_state,
    state_path,
)

EventFn = Callable[[dict[str, Any]], None]

_CANON_CAP = 4000
_VALID_MARKERS: frozenset[str] = frozenset(("bored", "confused", "reread", "hooked"))

_READER_SYSTEM = (
    "You are simulating a focus group of distinct readers. You report where a "
    "chapter caught or lost each reader as span-anchored events (bored, "
    "confused, reread, hooked) quoted verbatim from the text -- never as scores "
    "or numbers. You answer as the readers, not as an editor."
)


@dataclass
class ReadersRunResult:
    """Outcome of one `run_readers`. Counts + usage + notes; never prints."""

    run_id: str
    kind: str = "readers"
    calls: int = 0
    chapters_read: int = 0
    markers: int = 0
    misses: int = 0
    usage: Usage = field(default_factory=Usage)
    notes: list[str] = field(default_factory=list)
    state_path: str = ""
    stopped_at_cap: bool = False


def _emit(on_event: EventFn | None, event: dict[str, Any]) -> None:
    if on_event is not None:
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - a broken UI callback must not abort a run
            pass


def _chunk(items: list[str], k: int) -> list[list[str]]:
    k = max(1, k)
    return [items[i : i + k] for i in range(0, len(items), k)]


def render_reader_block(personas: list[Persona], states: dict[str, ReaderState]) -> str:
    """The verbatim persona-plus-state block for one batch's prompt."""
    parts: list[str] = []
    for p in personas:
        st = states.get(p.id) or ReaderState(persona=p.id)
        lines = [p.voice_block(), f"Currently remembers: {st.memory or 'nothing yet (first chapter)'}"]
        if st.expectations:
            lines.append("Expects next: " + "; ".join(st.expectations))
        if st.fatigue:
            lines.append("Growing tired of: " + st.fatigue)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def batch_completion(
    project: WritingProject,
    system: str,
    user: str,
    *,
    model: str | None = None,
    provider: Provider | None = None,
) -> tuple[dict[str, Any], Usage]:
    """One plain reader-role completion, tolerantly parsed to a JSON object.

    Shared by the chapter simulation and the bench pipeline (U5): both send a
    batched persona prompt and read a strict-JSON reply keyed by persona id.
    """
    text, usage = call_model(project, "reader", system, user, model=model, provider=provider)
    return extract_json(text), usage


def _parse_markers(raw: Any, persona: str, chapter: int, body: str) -> list[Marker]:
    out: list[Marker] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        mtype = str(item.get("type", "")).strip().lower()
        if mtype not in _VALID_MARKERS:
            continue
        quote = str(item.get("quote", "") or "")
        out.append(
            Marker(
                persona=persona,
                chapter=chapter,
                type=mtype,  # type: ignore[arg-type]
                quote=quote,
                note=str(item.get("note", "") or ""),
                span=locate_span(body, quote) if quote else None,
            )
        )
    return out


def _resolve_run(
    project: WritingProject,
    *,
    resume: bool,
    run_id: str | None,
    chapters: list[int],
    roster: list[str],
    max_calls: int,
    kind: RunKind = "readers",
) -> RunState:
    """Pick the run state to work on: an existing run when resuming, else a
    fresh one seeded with this run's plan."""
    if resume:
        target = run_id
        if target is None:
            prior = [s for s in list_runs(project) if s.kind == kind]
            target = prior[0].run_id if prior else None
        if target is not None and state_path(project, target).exists():
            state = load_state(project, target)
            state.budget.max_calls = max_calls  # a resumed run honors the new cap
            return state
    state = RunState(run_id=run_id or new_run_id(), kind=kind, chapters=chapters, roster=roster)
    state.budget.max_calls = max_calls
    return state


def run_readers(
    project: WritingProject,
    chapters: list[int] | None = None,
    roster: list[str] | None = None,
    model: str | None = None,
    provider: Provider | None = None,
    on_event: EventFn | None = None,
    max_calls: int | None = None,
    resume: bool = False,
    run_id: str | None = None,
) -> ReadersRunResult:
    """Simulate the roster reading `chapters` (default: all) in order, rolling
    each persona's state forward and logging span-anchored markers. Stops
    cleanly at the call cap; resumable. Never aborts on a bad batch."""
    cfg = project.config.readers
    personas = load_personas(project)
    roster_ids = list(roster) if roster else select_roster(personas, cfg.roster, cfg.roster_size)
    roster_ids = [pid for pid in roster_ids if pid in personas]
    if not roster_ids:
        raise ValueError("empty roster: no personas resolved (check readers.roster / roster_size)")

    all_chapters = chapters if chapters is not None else [c.number for c in project.chapters()]
    all_chapters = sorted(dict.fromkeys(all_chapters))
    if not all_chapters:
        raise ValueError("no chapters to read: the manuscript is empty")

    cap = max_calls if max_calls is not None else cfg.max_calls_per_run
    state = _resolve_run(
        project, resume=resume, run_id=run_id, chapters=all_chapters, roster=roster_ids, max_calls=cap
    )
    # A resumed run keeps its own chapter/roster plan; a fresh run adopts this call's.
    chapters_plan = state.chapters or all_chapters
    roster_plan = state.roster or roster_ids
    batch_personas = [[personas[pid] for pid in b] for b in _chunk(roster_plan, cfg.personas_per_call)]

    ledger = Ledger(project.root)
    result = ReadersRunResult(run_id=state.run_id, state_path=str(state_path(project, state.run_id)))
    save_state(project, state)
    ledger.append(
        "readers.run.start",
        target=state.run_id,
        chapters=len(chapters_plan),
        roster=len(roster_plan),
    )

    canon_digest = CanonStore(project).context_pack(max_chars=_CANON_CAP)

    while state.cursor < len(chapters_plan):
        if state.budget.calls_made >= cap:
            result.stopped_at_cap = True
            _emit(on_event, {"type": "budget.cap", "n": chapters_plan[state.cursor]})
            break
        chapter = chapters_plan[state.cursor]
        try:
            _, body = project.read_chapter(chapter)
        except ProjectError:
            result.notes.append(f"chapter {chapter} unreadable; skipped")
            state.cursor += 1
            save_state(project, state)
            continue

        _emit(on_event, {"type": "chapter.start", "n": chapter})
        # Work on copies so a mid-chapter stop never commits partial rolls.
        working = {pid: state.ensure_reader(pid).model_copy(deep=True) for pid in roster_plan}
        log = ChapterLog(chapter=chapter)
        aborted = False

        for batch in batch_personas:
            if state.budget.calls_made >= cap:
                aborted = True
                result.stopped_at_cap = True
                break
            batch_ids = [p.id for p in batch]
            user = _render_prompt(
                "readers_chapter.md",
                {
                    "chapter_number": f"{chapter:02d}",
                    "chapter_body": body,
                    "canon": canon_digest,
                    "readers_block": render_reader_block(batch, working),
                },
            )
            # Save intent BEFORE the call so a crash is resumable at this chapter.
            state.budget.calls_made += 1
            save_state(project, state)
            parsed, usage = batch_completion(
                project, _READER_SYSTEM, user, model=model, provider=provider
            )
            state.usage = state.usage + usage
            result.usage = result.usage + usage
            result.calls += 1
            ledger.append(
                "readers.run.call",
                target=state.run_id,
                chapter=chapter,
                personas=len(batch_ids),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )

            readers_obj = parsed.get("readers") if isinstance(parsed, dict) else None
            if not isinstance(readers_obj, dict):
                log.misses.extend(batch_ids)
                for pid in batch_ids:
                    working[pid].missed.append(chapter)
                result.misses += len(batch_ids)
                result.notes.append(f"ch {chapter}: batch [{', '.join(batch_ids)}] unparseable; recorded as misses")
                _emit(on_event, {"type": "batch.miss", "n": chapter, "personas": batch_ids})
                continue

            for p in batch:
                entry = readers_obj.get(p.id)
                if not isinstance(entry, dict):
                    log.misses.append(p.id)
                    working[p.id].missed.append(chapter)
                    result.misses += 1
                    result.notes.append(f"ch {chapter}: no reply for {p.id}; recorded as a miss")
                    continue
                markers = _parse_markers(entry.get("markers"), p.id, chapter, body)
                log.markers.extend(markers)
                result.markers += len(markers)
                memory = str(entry.get("memory", "") or "")
                log.reactions[p.id] = memory[:200]
                st = working[p.id]
                st.remember(chapter, memory)
                exp = entry.get("expectations")
                if isinstance(exp, list):
                    st.expectations = [str(e) for e in exp if str(e).strip()][:6]
                st.fatigue = str(entry.get("fatigue", "") or "")

        if aborted:
            # Discard this chapter's partial work; cursor stays put so resume
            # re-reads it cleanly. Budget already reflects the calls that ran.
            save_state(project, state)
            _emit(on_event, {"type": "budget.cap", "n": chapter})
            break

        state.reader_states.update(working)
        state.chapter_logs[chapter] = log
        state.cursor += 1
        result.chapters_read += 1
        save_state(project, state)
        _emit(on_event, {"type": "chapter.done", "n": chapter, "markers": len(log.markers)})

    ledger.append(
        "readers.run.done",
        target=state.run_id,
        calls=result.calls,
        markers=result.markers,
        misses=result.misses,
        stopped_at_cap=result.stopped_at_cap,
    )
    save_state(project, state)
    return result
