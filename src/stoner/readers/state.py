"""Crash-safe run state and marker models for reader simulation.

One JSON file per run at `.stoner/readers/runs/<run-id>/state.json`, following
the `BookState` pattern in `pipelines/book.py`: a pydantic model saved *before*
every model call, a file that will not parse backed up to `state.json.bak` and
replaced with fresh state, so a crash or Ctrl-C always leaves something a later
`--resume` can continue from. Budget counters live inside the state (invariant
10). Markers are span-anchored *events* only -- there is no numeric score field
anywhere in this module (invariant 1).

`ReaderState.memory` is a rolling, char-capped digest per persona: newest detail
appended, oldest lines dropped once past the cap, exactly mirroring
`canon/memory.py`'s cap-and-drop discipline -- persistent interiority without
context bloat.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..project import WritingProject
from ..types import Span, Usage

MarkerType = Literal["bored", "confused", "reread", "hooked"]
RunKind = Literal["readers", "bench"]

#: Negative marker types (drive the trouble-segment findings).
NEGATIVE_MARKERS: tuple[MarkerType, ...] = ("bored", "confused")

#: Per-persona rolling-memory cap in characters (oldest lines dropped past it).
MEMORY_CAP = 2000


class Marker(BaseModel):
    """One span-anchored attention event emitted by a persona for a chapter."""

    persona: str
    chapter: int
    type: MarkerType
    quote: str = ""
    note: str = ""
    span: Span | None = None  # resolved against the chapter body, null if unfound


class ReaderState(BaseModel):
    """Persistent per-persona interiority carried forward between chapters."""

    persona: str
    memory: str = ""  # rolling, char-capped digest in the reader's own voice
    expectations: list[str] = Field(default_factory=list)
    fatigue: str = ""
    missed: list[int] = Field(default_factory=list)  # chapters that degraded to a miss

    def remember(self, chapter: int, line: str, *, cap: int = MEMORY_CAP) -> None:
        """Append a chapter's memory line and drop oldest lines past the cap."""
        line = (line or "").strip()
        if not line:
            return
        entry = f"[ch {chapter}] {line}"
        self.memory = f"{self.memory}\n{entry}".strip() if self.memory else entry
        # Cap-and-drop: shed whole leading lines until under the cap (keep at
        # least the most recent line even if it alone exceeds the cap).
        lines = self.memory.split("\n")
        while len(lines) > 1 and len("\n".join(lines)) > cap:
            lines.pop(0)
        self.memory = "\n".join(lines)


class ChapterLog(BaseModel):
    """Everything one chapter produced across the roster."""

    chapter: int
    markers: list[Marker] = Field(default_factory=list)
    reactions: dict[str, str] = Field(default_factory=dict)  # persona id -> one line
    misses: list[str] = Field(default_factory=list)  # persona ids that degraded


class RunBudget(BaseModel):
    calls_made: int = 0
    max_calls: int = 150


class RunState(BaseModel):
    """Crash-resumable state for one readers (or bench) run."""

    run_id: str
    kind: RunKind = "readers"
    chapters: list[int] = Field(default_factory=list)
    roster: list[str] = Field(default_factory=list)
    reader_states: dict[str, ReaderState] = Field(default_factory=dict)
    # JSON object keys are strings; pydantic coerces int<->str on round-trip.
    chapter_logs: dict[int, ChapterLog] = Field(default_factory=dict)
    cursor: int = 0  # index into `chapters` of the next chapter to read
    budget: RunBudget = Field(default_factory=RunBudget)
    usage: Usage = Field(default_factory=Usage)
    # Bench-only: the comp being read against and the blind A/B mapping per
    # aligned chapter -- "a" means the manuscript is presented in slot A. Stored
    # BEFORE any call so the decode is reproducible.
    comp_slug: str = ""
    blind_map: dict[int, Literal["a", "b"]] = Field(default_factory=dict)
    # Bench-only: decoded per-chapter pick tallies (manuscript/comp/draws/total).
    bench_picks: dict[int, dict[str, int]] = Field(default_factory=dict)
    started_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def ensure_reader(self, persona: str) -> ReaderState:
        st = self.reader_states.get(persona)
        if st is None:
            st = ReaderState(persona=persona)
            self.reader_states[persona] = st
        return st


# ---------------------------------------------------------------------------
# Paths + persistence
# ---------------------------------------------------------------------------


def runs_dir(project: WritingProject) -> Path:
    return project.root / ".stoner" / "readers" / "runs"


def run_dir(project: WritingProject, run_id: str) -> Path:
    return runs_dir(project) / run_id


def state_path(project: WritingProject, run_id: str) -> Path:
    return run_dir(project, run_id) / "state.json"


def new_run_id(prefix: str = "run") -> str:
    """`<prefix>-<timestamp>`, matching the `.stoner/` id naming style."""
    return f"{prefix}-{int(time.time())}"


def load_state(project: WritingProject, run_id: str) -> RunState:
    """Load run state, tolerating corruption: a file that will not parse is
    backed up to `state.json.bak` and a fresh state is returned. A missing file
    also yields fresh state (callers that need existence check `state_path`)."""
    p = state_path(project, run_id)
    if not p.exists():
        return RunState(run_id=run_id)
    try:
        return RunState.model_validate_json(p.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        backup = p.parent / (p.name + ".bak")
        try:
            p.replace(backup)
        except OSError:
            pass
        return RunState(run_id=run_id)


def save_state(project: WritingProject, state: RunState) -> Path:
    """Atomically write run state (tmp file + replace) so a crash mid-write
    cannot truncate the live state file."""
    state.updated_at = time.time()
    p = state_path(project, state.run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def list_runs(project: WritingProject) -> list[RunState]:
    """All parseable run states, newest first."""
    base = runs_dir(project)
    if not base.exists():
        return []
    out: list[RunState] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        sp = d / "state.json"
        if not sp.exists():
            continue
        try:
            out.append(RunState.model_validate_json(sp.read_text(encoding="utf-8")))
        except (ValidationError, ValueError, OSError):
            continue
    out.sort(key=lambda s: s.started_at, reverse=True)
    return out
