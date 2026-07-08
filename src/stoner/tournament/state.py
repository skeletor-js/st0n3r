"""Crash-safe persisted state for one draft tournament.

One JSON file per tournament at `.stoner/tournaments/<id>.json` (take files
live in the sibling directory `.stoner/tournaments/<id>/`), mirroring the
`BookState` pattern in `pipelines/book.py`: pydantic model, saved before
every model call, a file that will not parse is backed up to `.bak` and
replaced with fresh state, so a crash or Ctrl-C always leaves something a
later `--resume` can continue from.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..project import WritingProject

TournamentStatus = Literal["drafting", "judging", "proposed", "applied", "abandoned"]


class TakeRecord(BaseModel):
    """One captured take: an angled draft stored under the tournament dir."""

    index: int
    angle: str = ""
    rel_path: str = ""  # project-relative path of the take file
    words: int = 0
    slop: float = 0.0
    steals: list[str] = Field(default_factory=list)  # named moves worth keeping
    note: str = ""


class Comparison(BaseModel):
    """One judged pair (both presentation orders folded into a verdict)."""

    a: int  # take index
    b: int  # take index
    verdict: Literal["a", "b", "draw"] = "draw"
    note: str = ""
    ts: float = Field(default_factory=time.time)


class TournamentState(BaseModel):
    """Everything a resumed run needs; saved before every model call."""

    id: str
    chapter: int = 0
    status: TournamentStatus = "drafting"
    planned_angles: list[str] = Field(default_factory=list)  # one per planned take
    takes: list[TakeRecord] = Field(default_factory=list)
    comparisons: list[Comparison] = Field(default_factory=list)
    ratings: dict[int, float] = Field(default_factory=dict)  # take index -> Elo
    proposed_winner: int | None = None
    # Budgets: `comparisons_done` counts individual judge calls (a pair costs
    # two, one per presentation order; parse retries count too).
    comparisons_done: int = 0
    max_comparisons: int = 24
    max_tokens_budget: int = 500_000
    tokens_used: int = 0
    # Chapter snapshot fingerprints (sha256 over the body only).
    chapter_existed: bool = True
    snapshot_sha: str = ""  # pre-tournament body ("" when no chapter existed)
    restored_sha: str = ""  # body left on disk after drafting; checked at apply
    votes: list[dict[str, Any]] = Field(default_factory=list)  # blind human votes
    notes: list[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    # -- convenience -----------------------------------------------------
    def take(self, index: int) -> TakeRecord | None:
        for t in self.takes:
            if t.index == index:
                return t
        return None

    def played_pairs(self) -> set[frozenset[int]]:
        return {frozenset((c.a, c.b)) for c in self.comparisons}


def tournaments_dir(project: WritingProject) -> Path:
    return project.root / ".stoner" / "tournaments"


def state_path(project: WritingProject, tournament_id: str) -> Path:
    return tournaments_dir(project) / f"{tournament_id}.json"


def takes_dir(project: WritingProject, tournament_id: str) -> Path:
    return tournaments_dir(project) / tournament_id


def snapshot_path(project: WritingProject, tournament_id: str) -> Path:
    return takes_dir(project, tournament_id) / "snapshot.md"


def new_tournament_id(chapter: int) -> str:
    """`ch-NN-<timestamp>`, matching the `.stoner/reviews/` naming style."""
    return f"ch-{chapter:02d}-{int(time.time())}"


def load_state(project: WritingProject, tournament_id: str) -> TournamentState:
    """Load tournament state, tolerating corruption: a file that will not
    parse is backed up to `<id>.json.bak` and a fresh state is returned.
    A missing file also yields fresh state (callers that need existence
    check `state_path(...).exists()` first)."""
    p = state_path(project, tournament_id)
    if not p.exists():
        return TournamentState(id=tournament_id)
    try:
        return TournamentState.model_validate_json(p.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        backup = p.parent / (p.name + ".bak")
        try:
            p.replace(backup)
        except OSError:
            pass
        return TournamentState(id=tournament_id)


def save_state(project: WritingProject, state: TournamentState) -> Path:
    state.updated_at = time.time()
    p = state_path(project, state.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return p


def list_states(project: WritingProject) -> list[TournamentState]:
    """All parseable tournament states, newest first."""
    base = tournaments_dir(project)
    if not base.exists():
        return []
    out: list[TournamentState] = []
    for f in sorted(base.glob("*.json")):
        if f.name.endswith(".bak"):
            continue
        try:
            out.append(TournamentState.model_validate_json(f.read_text(encoding="utf-8")))
        except (ValidationError, ValueError, OSError):
            continue
    out.sort(key=lambda s: s.created_at, reverse=True)
    return out
