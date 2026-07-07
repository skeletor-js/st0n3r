"""Autonomous book mode: draft every planned chapter, then run whole-book
review + revision rounds until the manuscript stops improving.

This is the autonovel "Phase 2-3" analog (`docs/research/RESEARCH.md`):
sequential chapter drafts, then a whole-manuscript review loop (bounded
rounds, stop on zero majors or a plateau). The loop is crash-resumable: a
`BookState` at `.stoner/book-state.json` records what has been drafted and
reviewed, and it is saved *before* every model call so a Ctrl-C always leaves
a state a later `run_book(resume=True)` can continue from. Chapter files are
never deleted.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..project import WritingProject, count_words
from ..providers.base import Provider
from ..review.book_review import BookReviewReport, run_book_review
from ..types import Severity, Usage
from .write import run_write

_BEATS_RE = re.compile(r"^ch-0*(\d+)\.md$")
_RESUME_STATUSES = ("draft", "revised", "final")
_MIN_WRITTEN_WORDS = 500
_MAJOR_SEVERITIES = (Severity.major, Severity.critical)

EventFn = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Persisted state
# ---------------------------------------------------------------------------


class ChapterDone(BaseModel):
    words: int = 0
    slop: float = 0.0
    reviewed: bool = False
    revision_cycles: int = 0


class BookBudget(BaseModel):
    chapters_this_run: int = 0
    max_chapters_per_run: int | None = None


class BookState(BaseModel):
    """Crash-resumable state for a whole-book run."""

    chapters_planned: list[int] = Field(default_factory=list)
    chapters_done: dict[int, ChapterDone] = Field(default_factory=dict)
    current: int | None = None
    phase: Literal["drafting", "reviewing", "done"] = "drafting"
    book_reviews: list[dict[str, Any]] = Field(default_factory=list)
    budget: BookBudget = Field(default_factory=BookBudget)
    started_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


def state_path(project: WritingProject) -> Path:
    return project.root / ".stoner" / "book-state.json"


def load_state(project: WritingProject) -> BookState:
    """Load book state, tolerating corruption: a file that will not parse is
    backed up to `book-state.json.bak` and a fresh state is returned."""
    p = state_path(project)
    if not p.exists():
        return BookState()
    try:
        return BookState.model_validate_json(p.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        backup = p.parent / (p.name + ".bak")
        try:
            p.replace(backup)
        except OSError:
            pass
        return BookState()


def save_state(project: WritingProject, state: BookState) -> Path:
    state.updated_at = time.time()
    p = state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class BookResult:
    chapters_written: int = 0
    total_words: int = 0
    review_rounds: int = 0
    remaining_major_findings: int = 0
    remaining_critical_findings: int = 0
    state_path: str = ""
    usage: Usage = field(default_factory=Usage)


# ---------------------------------------------------------------------------
# Planning / resume helpers
# ---------------------------------------------------------------------------


def planned_chapters(project: WritingProject) -> list[int]:
    """Chapter numbers with a beat sheet (`outline/beats/ch-NN.md`) or an
    existing manuscript chapter, sorted ascending."""
    nums: set[int] = set()
    beats_dir = project.root / "outline" / "beats"
    if beats_dir.exists():
        for f in beats_dir.iterdir():
            m = _BEATS_RE.match(f.name)
            if m:
                nums.add(int(m.group(1)))
    for c in project.chapters():
        nums.add(c.number)
    return sorted(nums)


def _already_written(project: WritingProject, n: int, state: BookState, resume: bool) -> bool:
    """Resume predicate: a chapter counts as written if the state already
    records it, or the file already holds substantial prose."""
    if not resume:
        return False
    if n in state.chapters_done:
        return True
    try:
        fm, body = project.read_chapter(n)
    except Exception:  # noqa: BLE001 - no/broken file means not yet written
        return False
    if count_words(body) <= _MIN_WRITTEN_WORDS:
        return False
    return str(fm.get("status", "draft")) in _RESUME_STATUSES


def _emit(on_event: EventFn | None, event: dict[str, Any]) -> None:
    if on_event is not None:
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - a broken UI callback must not abort the run
            pass


# ---------------------------------------------------------------------------
# Review + revise phase
# ---------------------------------------------------------------------------


def _review_and_revise(
    project: WritingProject,
    state: BookState,
    result: BookResult,
    model: str | None,
    provider: Provider | None,
    max_review_rounds: int,
    on_event: EventFn | None,
) -> BookReviewReport | None:
    """Run up to `max_review_rounds` whole-book review rounds, revising the
    worst-hit chapters between rounds. Stops early on zero majors or when the
    major count fails to decrease (plateau). Returns the last report."""
    from ..review.revise import revise_chapter

    state.phase = "reviewing"
    save_state(project, state)

    prev_majors: int | None = None
    last_report: BookReviewReport | None = None
    rounds = 0
    while rounds < max_review_rounds:
        report = run_book_review(project, provider=provider)
        result.usage = result.usage + report.usage
        rounds += 1
        result.review_rounds += 1
        last_report = report
        state.book_reviews.append(
            {
                "ts": report.created_at,
                "findings": len(report.findings),
                "major_count": report.major_count,
                "verdict": report.verdict,
            }
        )
        save_state(project, state)
        _emit(
            on_event,
            {
                "type": "review.round",
                "round": rounds,
                "majors": report.major_count,
                "verdict": report.verdict,
            },
        )

        majors = report.major_count
        if majors == 0:
            break
        if prev_majors is not None and majors >= prev_majors:
            # Plateau: another round is unlikely to help.
            break

        # Revise the worst-hit chapters with their own major/critical findings.
        grouped = report.by_chapter()
        for n in sorted(grouped):
            chapter_majors = [f for f in grouped[n] if f.severity in _MAJOR_SEVERITIES]
            if not chapter_majors:
                continue
            # `reason` tags the draft snapshot; passed only when the
            # (replaceable, late-imported) callable accepts it.
            revise_kwargs: dict[str, Any] = {}
            if "reason" in inspect.signature(revise_chapter).parameters:
                revise_kwargs["reason"] = "book-revise"
            try:
                revision = revise_chapter(
                    project, n, chapter_majors, provider=provider, **revise_kwargs
                )
            except (ValueError, RuntimeError):
                continue
            result.usage = result.usage + revision.usage
            done = state.chapters_done.get(n) or ChapterDone()
            done.reviewed = True
            done.revision_cycles += 1
            try:
                _, body = project.read_chapter(n)
                done.words = count_words(body)
            except Exception:  # noqa: BLE001
                pass
            state.chapters_done[n] = done
            save_state(project, state)
            _emit(on_event, {"type": "chapter.revised", "n": n, "findings": len(chapter_majors)})

        prev_majors = majors

    return last_report


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def run_book(
    project: WritingProject,
    model: str | None = None,
    provider: Provider | None = None,
    max_chapters: int | None = None,
    review_every: int = 4,
    max_review_rounds: int = 2,
    resume: bool = True,
    on_event: EventFn | None = None,
    max_minutes: float | None = None,
) -> BookResult:
    """Draft every planned, unwritten chapter in order, running whole-book
    review + revision rounds every `review_every` chapters and once at the
    end. State is saved before every model call, so a crash/Ctrl-C is always
    resumable with `resume=True`. Chapter files are never deleted.
    """
    planned = planned_chapters(project)
    if not planned:
        raise ValueError(
            "No chapters to write: no beat sheets found under outline/beats/ "
            "and no existing chapters. Run `stoner foundation` first or add "
            "beat sheets (`stoner beats <n>`)."
        )

    state = load_state(project) if resume else BookState()
    state.chapters_planned = planned
    state.phase = "drafting"
    state.budget.chapters_this_run = 0
    state.budget.max_chapters_per_run = max_chapters
    save_state(project, state)

    result = BookResult(state_path=str(state_path(project)))
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    drafted_since_review = 0

    for n in planned:
        if _already_written(project, n, state, resume):
            continue
        if max_chapters is not None and result.chapters_written >= max_chapters:
            break
        if deadline is not None and time.monotonic() >= deadline:
            _emit(on_event, {"type": "budget.timeout", "n": n})
            break

        # Save intent BEFORE the model call so a crash mid-draft is resumable.
        state.current = n
        state.budget.chapters_this_run = result.chapters_written
        save_state(project, state)

        write = run_write(project, n, model=model, provider=provider)
        result.usage = result.usage + write.usage
        result.chapters_written += 1
        drafted_since_review += 1

        state.chapters_done[n] = ChapterDone(
            words=write.words,
            slop=write.slop_after,
            reviewed=False,
            revision_cycles=write.revision_loops,
        )
        state.current = None
        state.budget.chapters_this_run = result.chapters_written
        save_state(project, state)
        _emit(
            on_event,
            {"type": "chapter.done", "n": n, "words": write.words, "slop": write.slop_after},
        )

        if review_every > 0 and drafted_since_review >= review_every:
            _review_and_revise(
                project, state, result, model, provider, max_review_rounds, on_event
            )
            drafted_since_review = 0
            state.phase = "drafting"
            save_state(project, state)

    # Final whole-book review pass (skip only if nothing exists to review).
    if project.chapters():
        report = _review_and_revise(
            project, state, result, model, provider, max_review_rounds, on_event
        )
        if report is not None:
            result.remaining_major_findings = report.major_count
            result.remaining_critical_findings = report.critical_count

    state.phase = "done"
    state.current = None
    save_state(project, state)

    result.total_words = sum(c.words for c in project.chapters())
    return result
