"""The chapter-by-chapter pacing judge: tension labels, changes-hands
ledger, and beat-drift verdicts.

Split pure from effectful like `review/passes.py` vs `runner.py`:
`build_judge_prompt` and `parse_judgment` are pure prompt-builder/parser
functions; `judge_chapters` owns the loop, the provider call (a plain
no-tools completion against the `reviewer` role via
`pipelines/common.call_model`), the hash-keyed cache at
`.stoner/pacing-state.json` (saved BEFORE every model call, `.bak` on
corruption -- the BookState pattern), and the per-chapter `pacing.judge`
ledger entries.

Hard invariants honored here:
- Tension labels are forced-relative (opens/rises/holds/sags), never numeric.
- The judge never sees more than one chapter body per call: context for
  "what came before" is the previous chapter's rolling memory summary only.
- A failing or unparseable call degrades that chapter to `unjudged` plus one
  info finding (the runner degradation pattern) -- never aborts the run,
  and unjudged results are not cached so a re-run retries them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..ledger import Ledger
from ..pipelines.common import call_model, render_prompt
from ..project import WritingProject
from ..providers.base import Provider, ProviderError
from ..review.passes import extract_json
from ..types import Finding, Severity, Usage
from .data import ChapterData, chapter_hash

EventFn = Callable[[dict[str, Any]], None]

#: Labels the judge may return; "opens" only for the first chapter,
#: "unjudged" only assigned by the harness on failure.
TENSION_LABELS = ("opens", "rises", "holds", "sags")
_BEAT_VERDICTS = ("landed", "drifted", "missed")
_MAX_BEATS = 25
_MAX_CHANGES = 25


# ---------------------------------------------------------------------------
# Judgment + persisted state models
# ---------------------------------------------------------------------------


class BeatVerdict(BaseModel):
    beat: str
    verdict: Literal["landed", "drifted", "missed"]
    note: str = ""


class ChapterJudgment(BaseModel):
    chapter: int
    tension: str = "unjudged"
    tension_why: str = ""
    changes_hands: list[str] = Field(default_factory=list)
    beats: list[BeatVerdict] = Field(default_factory=list)
    cached: bool = False


class _CachedJudgment(BaseModel):
    content_hash: str
    judgment: ChapterJudgment


class PacingState(BaseModel):
    """Judge cache: chapter number -> content hash + judgment."""

    chapters: dict[int, _CachedJudgment] = Field(default_factory=dict)
    updated_at: float = Field(default_factory=time.time)


def state_path(project: WritingProject) -> Path:
    return project.root / ".stoner" / "pacing-state.json"


def load_state(project: WritingProject) -> PacingState:
    """Load judge state, tolerating corruption: an unparseable file is
    backed up to `pacing-state.json.bak` and a fresh state returned."""
    p = state_path(project)
    if not p.exists():
        return PacingState()
    try:
        return PacingState.model_validate_json(p.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        backup = p.parent / (p.name + ".bak")
        try:
            p.replace(backup)
        except OSError:
            pass
        return PacingState()


def save_state(project: WritingProject, state: PacingState) -> Path:
    state.updated_at = time.time()
    p = state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Pure prompt builder / parser
# ---------------------------------------------------------------------------


def build_judge_prompt(chapter: ChapterData, prev_summary: str) -> tuple[str, str]:
    """(system, user) for judging one chapter. The user prompt carries this
    chapter's body, the previous chapter's memory summary, and this
    chapter's beat sheet -- never another chapter's body (invariant 4)."""
    system = render_prompt("pacing_judge.md", {"project_name": ""})
    parts = [f"## Chapter {chapter.number} (full text)\n\n{chapter.body}"]
    if prev_summary:
        parts.append(f"## Previous Chapter (memory summary)\n\n{prev_summary}")
    else:
        parts.append(
            "## Previous Chapter\n\nThis is the first chapter (use the `opens` tension label)."
            if chapter.number <= 1
            else "## Previous Chapter\n\n(no summary available)"
        )
    parts.append(
        f"## Beat Sheet for Chapter {chapter.number}\n\n{chapter.beats_text}"
        if chapter.beats_text.strip()
        else "## Beat Sheet\n\nnone"
    )
    parts.append("## Task\n\nJudge this chapter per your instructions. STRICT JSON only.")
    return system, "\n\n".join(parts)


def _system_prompt(project_name: str) -> str:
    return render_prompt("pacing_judge.md", {"project_name": project_name})


def parse_judgment(text: str, chapter: int, first: bool = False) -> ChapterJudgment | None:
    """Tolerantly parse the judge's response. Returns None when the response
    is unusable (no JSON, or an invalid tension label) -- the caller
    degrades that chapter to `unjudged`."""
    data = extract_json(text)
    if not data:
        return None
    tension = str(data.get("tension", "")).strip().lower()
    allowed = TENSION_LABELS if first else TENSION_LABELS[1:]
    if tension not in allowed:
        return None

    changes: list[str] = []
    raw_changes = data.get("changes_hands")
    if isinstance(raw_changes, list):
        changes = [str(c).strip() for c in raw_changes[:_MAX_CHANGES] if str(c).strip()]

    beats: list[BeatVerdict] = []
    raw_beats = data.get("beats")
    if isinstance(raw_beats, list):
        for item in raw_beats[:_MAX_BEATS]:
            if not isinstance(item, dict):
                continue
            verdict = str(item.get("verdict", "")).strip().lower()
            if verdict not in _BEAT_VERDICTS:
                continue
            beats.append(
                BeatVerdict(
                    beat=str(item.get("beat", "")),
                    verdict=verdict,  # type: ignore[arg-type]
                    note=str(item.get("note", "")),
                )
            )

    return ChapterJudgment(
        chapter=chapter,
        tension=tension,
        tension_why=str(data.get("tension_why", "")).strip(),
        changes_hands=changes,
        beats=beats,
    )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def judge_chapters(
    project: WritingProject,
    chapters: list[ChapterData],
    model: str | None = None,
    provider: Provider | None = None,
    on_event: EventFn | None = None,
) -> tuple[list[ChapterJudgment], list[Finding], Usage]:
    """Judge every chapter, one model call each, resuming from the cache.

    A chapter whose content hash matches the cached entry is returned with
    `cached=True` and costs no provider call. State is flushed to disk
    before every model call so an interrupted run resumes. Failures degrade
    to `tension="unjudged"` plus one info finding and are NOT cached.
    """
    state = load_state(project)
    ledger = Ledger(project.root)
    system = _system_prompt(project.config.project_name)
    model_str = model if model is not None else project.config.models.reviewer

    judgments: list[ChapterJudgment] = []
    findings: list[Finding] = []
    usage = Usage()

    for i, ch in enumerate(chapters):
        content_hash = chapter_hash(ch)
        target = f"manuscript/ch-{ch.number:02d}.md"
        cached = state.chapters.get(ch.number)
        if cached is not None and cached.content_hash == content_hash:
            judgments.append(cached.judgment.model_copy(update={"cached": True}))
            ledger.append("pacing.judge", target=target, chapter=ch.number, cached=True)
            continue

        # Save intent BEFORE the model call so a crash is resumable.
        save_state(project, state)

        prev_summary = chapters[i - 1].memory_summary if i > 0 else ""
        _, user = build_judge_prompt(ch, prev_summary)
        judgment: ChapterJudgment | None = None
        error = ""
        spent = Usage()
        try:
            text, spent = call_model(
                project, "reviewer", system=system, user=user, model=model, provider=provider
            )
            usage = usage + spent
            judgment = parse_judgment(text, ch.number, first=(i == 0))
            if judgment is None:
                error = "judge response did not contain a valid judgment"
        except ProviderError as e:
            error = str(e)

        if judgment is None:
            judgment = ChapterJudgment(chapter=ch.number, tension="unjudged")
            findings.append(
                Finding(
                    source="pacing:judge",
                    severity=Severity.info,
                    category=f"ch-{ch.number:02d}:unjudged",
                    issue=f"chapter {ch.number} could not be judged: {error}",
                )
            )
        else:
            state.chapters[ch.number] = _CachedJudgment(
                content_hash=content_hash, judgment=judgment
            )
            save_state(project, state)

        judgments.append(judgment)
        ledger.append(
            "pacing.judge",
            target=target,
            chapter=ch.number,
            cached=False,
            model=model_str,
            tension=judgment.tension,
            input_tokens=spent.input_tokens,
            output_tokens=spent.output_tokens,
        )
        if on_event is not None:
            try:
                on_event({"type": "pacing.judged", "n": ch.number, "tension": judgment.tension})
            except Exception:  # noqa: BLE001 - a broken callback must not abort the run
                pass

    save_state(project, state)
    return judgments, findings, usage
