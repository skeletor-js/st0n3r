"""Tournament orchestrator: draft -> judge -> propose, resume support, and
the human-confirmed apply with optional grafting.

`run_tournament` never writes the manuscript beyond the per-take
snapshot/restore cycle; its output is a PROPOSED winner (the ceiling of
autonomy, invariant 2). `apply_winner` is the human-confirmed step: it
verifies the chapter still matches the tournament's post-run fingerprint
(else requires force), optionally grafts the losers' steals, and writes the
result through the archaeology chokepoint (`snapshot_write_chapter`, reason
`tournament-graft`), never running the slop gate or auto-revise loop --
post-apply polish belongs to the normal review/revise flow.

This module is the seam for pipeline integration (plan 011): it never
prints, and `pipelines/write.py` / `pipelines/book.py` are not touched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..archaeology.snapshots import body_hash, snapshot_write_chapter
from ..ledger import Ledger
from ..project import ProjectError, WritingProject, count_words, split_frontmatter
from ..providers.base import Provider
from ..types import Usage
from .angles import select_angles, takes_for_chapter
from .graft import graft_winner
from .judge import run_judging
from .state import (
    TournamentState,
    load_state,
    new_tournament_id,
    save_state,
    snapshot_path,
    state_path,
)
from .takes import draft_takes, read_take_body, snapshot_chapter
from .taste import angle_weights, digest, load_taste

EventFn = Callable[[dict[str, Any]], None]

_GUARDED_STATUSES = ("revised", "final")


@dataclass
class TournamentResult:
    """What one `run_tournament` call did. Mirrors `WriteResult`: data only,
    never prints."""

    id: str
    chapter: int
    takes: int = 0
    comparisons: int = 0
    proposed_winner: int | None = None
    status: str = ""
    state_path: str = ""
    usage: Usage = field(default_factory=Usage)
    notes: list[str] = field(default_factory=list)


@dataclass
class ApplyResult:
    """What one `apply_winner` call did."""

    id: str
    chapter: int
    take: int
    grafted: bool = False
    words: int = 0
    usage: Usage = field(default_factory=Usage)
    notes: list[str] = field(default_factory=list)


def _guard_chapter_status(project: WritingProject, chapter: int, force: bool) -> None:
    try:
        fm, _body = project.read_chapter(chapter)
    except ProjectError:
        return  # no chapter yet: nothing to guard
    status = str(fm.get("status", "draft"))
    if status in _GUARDED_STATUSES and not force:
        raise ValueError(
            f"chapter {chapter} has status '{status}'; running a tournament "
            "over reviewed work needs --force"
        )


def run_tournament(
    project: WritingProject,
    chapter: int,
    takes: int | None = None,
    model: str | None = None,
    provider: Provider | None = None,
    resume_id: str | None = None,
    force: bool = False,
    max_comparisons: int | None = None,
    on_event: EventFn | None = None,
) -> TournamentResult:
    """Draft N angled takes of `chapter`, judge them blind, propose a winner.

    The manuscript is left exactly as found (restored after every take);
    nothing is applied until a human runs `apply_winner`. State is saved
    before every model call, so a crash is resumable with `resume_id`.
    """
    ledger = Ledger(project.root)
    cfg = project.config.tournament
    profile = load_taste(project)

    if resume_id is not None:
        if not state_path(project, resume_id).exists():
            raise ValueError(f"no tournament state found for id {resume_id!r}")
        state = load_state(project, resume_id)
        if not state.planned_angles:
            raise ValueError(
                f"tournament state for {resume_id!r} is unusable (was it corrupt?); "
                "start a fresh run"
            )
        ledger.append(
            "tournament.resume",
            target=project.chapter_rel(state.chapter),
            tournament=state.id,
            status=state.status,
        )
    else:
        _guard_chapter_status(project, chapter, force)
        n = takes_for_chapter(project, chapter, takes)
        angles = select_angles(n, cfg, weights=angle_weights(profile) or None)
        state = TournamentState(
            id=new_tournament_id(chapter),
            chapter=chapter,
            planned_angles=[a.name for a in angles],
            max_comparisons=max_comparisons if max_comparisons is not None else cfg.max_comparisons,
            max_tokens_budget=cfg.max_tokens_budget,
        )
        snapshot_chapter(project, state)
        save_state(project, state)
        ledger.append(
            "tournament.start",
            target=project.chapter_rel(chapter),
            tournament=state.id,
            takes=n,
            angles=state.planned_angles,
        )

    result = TournamentResult(
        id=state.id, chapter=state.chapter, state_path=str(state_path(project, state.id))
    )

    if state.status == "drafting":
        result.usage += draft_takes(
            project, state, model=model, provider=provider, on_event=on_event
        )
    if state.status in ("drafting", "judging"):
        result.usage += run_judging(
            project,
            state,
            taste_digest=digest(profile),
            model=model,
            provider=provider,
            on_event=on_event,
        )
    else:
        state.notes.append(f"resume: tournament already {state.status}; nothing to do")
        save_state(project, state)

    result.takes = len(state.takes)
    result.comparisons = len(state.comparisons)
    result.proposed_winner = state.proposed_winner
    result.status = state.status
    result.notes = list(state.notes)
    return result


def apply_winner(
    project: WritingProject,
    tournament_id: str,
    take: int | None = None,
    graft: bool | None = None,
    force: bool = False,
    model: str | None = None,
    provider: Provider | None = None,
) -> ApplyResult:
    """Write a tournament take into the manuscript -- the human-confirmed
    step. `take` overrides the proposal (the proposal is advisory). The
    chapter must still match the tournament's post-run fingerprint unless
    `force`. Grafting (config default, `graft=` override) folds the losing
    takes' steals into the winner; a refused graft applies the raw winner
    with a note. The written chapter gets `status: draft` -- the slop gate
    and auto-revise loop deliberately do not run here."""
    if not state_path(project, tournament_id).exists():
        raise ValueError(f"no tournament state found for id {tournament_id!r}")
    state = load_state(project, tournament_id)
    if state.status not in ("proposed", "applied"):
        raise ValueError(
            f"tournament {tournament_id} is {state.status!r}; only a proposed "
            "tournament can be applied (finish the run first)"
        )

    winner_idx = take if take is not None else state.proposed_winner
    if winner_idx is None:
        raise ValueError(f"tournament {tournament_id} has no proposed winner")
    winner = state.take(winner_idx)
    if winner is None:
        raise ValueError(f"tournament {tournament_id} has no take {winner_idx}")

    # Drift guard (R15): the chapter must be as the tournament left it.
    try:
        _fm, current_body = project.read_chapter(state.chapter)
        current_sha = body_hash(current_body)
    except ProjectError:
        current_sha = ""
    if current_sha != state.restored_sha and not force:
        raise ValueError(
            f"chapter {state.chapter} changed since tournament {tournament_id} "
            "ran; re-run the tournament or apply with --force"
        )

    result = ApplyResult(id=tournament_id, chapter=state.chapter, take=winner_idx)
    ledger = Ledger(project.root)
    winner_body = read_take_body(project, winner)

    do_graft = graft if graft is not None else project.config.tournament.graft
    steals = [s for t in state.takes if t.index != winner_idx for s in t.steals]
    body = winner_body
    if do_graft and steals:
        save_state(project, state)  # save before the model call
        grafted = graft_winner(
            project, state.chapter, winner_body, steals, model=model, provider=provider
        )
        result.usage += grafted.usage
        if grafted.body is not None:
            body = grafted.body
            result.grafted = True
            ledger.append(
                "tournament.graft",
                target=project.chapter_rel(state.chapter),
                tournament=tournament_id,
                take=winner_idx,
                steals=len(steals),
            )
        else:
            result.notes.append(grafted.note)
            state.notes.append(grafted.note)

    # Frontmatter: keep the pre-tournament header (title, pov) when one
    # existed; the applied chapter is a fresh draft either way.
    if state.chapter_existed:
        fm, _old = split_frontmatter(
            snapshot_path(project, tournament_id).read_text(encoding="utf-8")
        )
    else:
        from ..canon.scaffold import new_chapter_stub

        fm, _stub = new_chapter_stub(state.chapter)
    fm = dict(fm)
    fm["status"] = "draft"

    snapshot_write_chapter(
        project,
        state.chapter,
        fm,
        body,
        reason="tournament-graft",
        detail={"tournament": tournament_id, "take": winner_idx, "grafted": result.grafted},
    )
    result.words = count_words(body)

    state.status = "applied"
    _fm3, new_body = project.read_chapter(state.chapter)
    state.restored_sha = body_hash(new_body)
    save_state(project, state)
    ledger.append(
        "tournament.apply",
        target=project.chapter_rel(state.chapter),
        tournament=tournament_id,
        take=winner_idx,
        grafted=result.grafted,
        words=result.words,
    )
    return result
