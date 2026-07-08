"""Take drafting: snapshot the chapter, draft N angled takes through
`draft_chapter`, capture each into `.stoner/tournaments/<id>/take-NN.md`,
and restore the chapter after every take.

`draft_chapter` (pipelines/write.py) owns the tool loop, the text-only
single-shot degradation, and the 200-word refusal guard, and its agent
writes to the canonical chapter path -- so the tournament snapshots the
chapter before starting, captures the chapter file into take storage after
each draft, and restores the snapshot (or the `new_chapter_stub` scaffold
when no chapter existed) after each take. No chapter file is ever deleted.

Restores go through the archaeology chokepoint (`snapshot_write_chapter`,
reason `tournament-restore`) rather than a plain file write: the chokepoint
keeps the drafts manifest's drift detection coherent (a plain write would
make the next harness write mis-detect the restore as a human edit) and
preserves each take in draft archaeology as a bonus.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..archaeology.snapshots import snapshot_write_chapter
from ..canon.scaffold import new_chapter_stub
from ..canon.store import CanonStore
from ..ledger import Ledger
from ..project import (
    ProjectError,
    WritingProject,
    count_words,
    join_frontmatter,
    split_frontmatter,
)
from ..providers.base import Provider
from ..slop import run_slop
from ..types import Usage
from .state import TakeRecord, TournamentState, save_state, snapshot_path, takes_dir

EventFn = Callable[[dict[str, Any]], None]

_MIN_TAKES = 2


def _emit(on_event: EventFn | None, event: dict[str, Any]) -> None:
    if on_event is not None:
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - a broken UI callback must not abort the run
            pass


def _angle_task(number: int, angle_name: str, instruction: str) -> str:
    """Angle-specific drafting task, layered on the writer.md system prompt.

    Works on both provider paths: for tool providers it replaces the default
    agent task (the write_chapter mention keeps the tool loop on rails); for
    text-only providers it replaces the single-shot user message, so it must
    repeat the "reply with ONLY the chapter prose" contract from
    `draft_chapter`.
    """
    return (
        f"Draft chapter {number} now, in full, as finished prose -- from "
        f"this specific angle ({angle_name}):\n\n{instruction}\n\n"
        "Everything else you need -- premise, style, canon, beat sheet, "
        "memory, the tail of the previous chapter -- is in your "
        "instructions above. If you can save with the write_chapter tool "
        f"(number={number}), do so; otherwise reply with ONLY the chapter "
        "prose: no title line, no notes, no commentary before or after."
    )


def _take_rel(project: WritingProject, tournament_id: str, index: int) -> str:
    p = takes_dir(project, tournament_id) / f"take-{index:02d}.md"
    return str(p.relative_to(project.root))


def _restore_chapter(project: WritingProject, state: TournamentState) -> None:
    """Put the manuscript back exactly as the tournament found it (or the
    new-chapter stub when no chapter existed before). Routed through the
    snapshot chokepoint -- see module docstring."""
    if state.chapter_existed:
        snap = snapshot_path(project, state.id)
        fm, body = split_frontmatter(snap.read_text(encoding="utf-8"))
        snapshot_write_chapter(project, state.chapter, fm, body, reason="tournament-restore")
    else:
        fm, body = new_chapter_stub(state.chapter)
        snapshot_write_chapter(project, state.chapter, fm, body, reason="tournament-restore")


def snapshot_chapter(project: WritingProject, state: TournamentState) -> None:
    """Record the pre-tournament chapter (full text + body fingerprint), or
    its absence, before any drafting happens."""
    from ..archaeology.snapshots import body_hash

    try:
        raw = project.read(project.chapter_rel(state.chapter))
    except ProjectError:
        state.chapter_existed = False
        state.snapshot_sha = ""
        return
    state.chapter_existed = True
    _fm, body = split_frontmatter(raw)
    state.snapshot_sha = body_hash(body)
    snap = snapshot_path(project, state.id)
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(raw, encoding="utf-8")


def draft_takes(
    project: WritingProject,
    state: TournamentState,
    model: str | None = None,
    provider: Provider | None = None,
    on_event: EventFn | None = None,
) -> Usage:
    """Draft every planned take that has not been captured yet.

    Per take: save state, draft via `draft_chapter` with the angle task,
    capture the chapter into take storage (frontmatter: angle, words, slop),
    ledger `tournament.take`, restore the snapshot. A refusal-guard failure
    records a note and continues; fewer than 2 captured takes raises.
    """
    from ..pipelines.write import draft_chapter  # late: heavy module

    usage = Usage()
    ledger = Ledger(project.root)
    bw, bp = CanonStore(project).banned_terms()
    done = {t.index for t in state.takes}

    from .angles import all_angles

    instructions = {a.name: a.instruction for a in all_angles(project.config.tournament)}

    for i, angle_name in enumerate(state.planned_angles, start=1):
        if i in done:
            continue
        # Save intent BEFORE the model call so a crash mid-draft is resumable.
        save_state(project, state)
        task = _angle_task(state.chapter, angle_name, instructions.get(angle_name, angle_name))
        try:
            usage += draft_chapter(
                project, state.chapter, model=model, provider=provider, task=task
            )
        except RuntimeError as exc:
            state.notes.append(f"take {i} ({angle_name}) failed: {exc}")
            save_state(project, state)
            _emit(on_event, {"type": "take.failed", "take": i, "angle": angle_name})
            continue

        raw = project.read(project.chapter_rel(state.chapter))
        _fm, body = split_frontmatter(raw)
        slop = run_slop(
            raw, path=project.chapter_rel(state.chapter), banned_words=bw, banned_phrases=bp
        ).score
        words = count_words(body)
        rel = _take_rel(project, state.id, i)
        take_fm = {
            "tournament": state.id,
            "take": i,
            "angle": angle_name,
            "words": words,
            "slop": round(slop, 1),
            "created_at": time.time(),
        }
        project.write(rel, join_frontmatter(take_fm, body))
        state.takes.append(
            TakeRecord(index=i, angle=angle_name, rel_path=rel, words=words, slop=slop)
        )
        save_state(project, state)
        ledger.append(
            "tournament.take",
            target=rel,
            tournament=state.id,
            chapter=state.chapter,
            take=i,
            angle=angle_name,
            words=words,
        )
        _emit(on_event, {"type": "take.done", "take": i, "angle": angle_name, "words": words})
        _restore_chapter(project, state)

    # Fingerprint of what drafting left on disk; `apply` verifies against it.
    from ..archaeology.snapshots import body_hash

    try:
        _fm2, body2 = project.read_chapter(state.chapter)
        state.restored_sha = body_hash(body2)
    except ProjectError:
        state.restored_sha = ""
    save_state(project, state)

    if len(state.takes) < _MIN_TAKES:
        raise RuntimeError(
            f"tournament {state.id} captured only {len(state.takes)} take(s); "
            "at least 2 are needed to judge. See notes in the state file."
        )
    return usage


def read_take_body(project: WritingProject, take: TakeRecord) -> str:
    """Body text of a captured take (frontmatter stripped)."""
    _fm, body = split_frontmatter(project.read(take.rel_path))
    return body
