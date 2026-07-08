"""Chapter-aligned blind pairwise benchmark against a public-domain comp.

`run_bench` aligns manuscript chapter n with comp chapter n up to the shorter
book, and for each aligned chapter presents the pair BLIND: the A/B order is
randomized per chapter and stored in run state BEFORE any call (so the decode is
reproducible), reusing the U3 batch machinery with `readers_bench.md`. Each
persona returns a pick; picks are decoded through the stored mapping and
aggregated arithmetically into per-chapter and overall win rates (invariant 1).

If the Draft Tournaments rating utilities are importable, they upgrade the
aggregation (Elo over the pooled manuscript-vs-comp games); absent, bench
degrades silently to win-rate arithmetic with a result note (invariant: no hard
dependency on plan 003). The guarded import goes through `_import_rating`, which
tests monkeypatch to exercise the absence path.
"""

from __future__ import annotations

import importlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from ..ledger import Ledger
from ..project import ProjectError, WritingProject
from ..providers.base import Provider
from ..types import Usage
from .comps import load_comp, load_comp_chapters
from .personas import Persona, load_personas, select_roster
from .simulate import _chunk, batch_completion, render_reader_block
from .state import RunState, load_state, new_run_id, run_dir, save_state, state_path

EventFn = Callable[[dict[str, Any]], None]

_BENCH_SYSTEM = (
    "You are simulating a focus group of distinct readers judging two passages "
    "blind. Each reader picks the passage that held their attention better and "
    "says where each one lost them, in verbatim quotes -- never a numeric score."
)


@dataclass
class BenchResult:
    run_id: str
    comp: str
    chapters_aligned: int = 0
    comp_shorter: bool = False
    per_chapter: list[dict[str, Any]] = field(default_factory=list)
    manuscript_win_rate: float = 0.0
    aggregation: str = "win-rate"  # or "elo"
    ratings: dict[str, float] = field(default_factory=dict)
    calls: int = 0
    usage: Usage = field(default_factory=Usage)
    notes: list[str] = field(default_factory=list)
    state_path: str = ""
    stopped_at_cap: bool = False


def _import_rating() -> ModuleType | None:
    """Guarded import of the tournament rating utilities. Returns None when the
    tournament namespace is absent -- monkeypatched in tests to exercise that."""
    try:
        return importlib.import_module("stoner.tournament.rating")
    except ImportError:
        return None


def _emit(on_event: EventFn | None, event: dict[str, Any]) -> None:
    if on_event is not None:
        try:
            on_event(event)
        except Exception:  # noqa: BLE001
            pass


def _plan_blind_map(state: RunState, chapters: list[int]) -> None:
    """Assign each aligned chapter a blind slot for the manuscript ("a"/"b"),
    deterministically seeded by the run id, only for chapters not yet mapped."""
    rng = random.Random(state.run_id)
    for ch in chapters:
        if ch not in state.blind_map:
            state.blind_map[ch] = "a" if rng.random() < 0.5 else "b"


def run_bench(
    project: WritingProject,
    comp: str,
    chapters: list[int] | None = None,
    roster: list[str] | None = None,
    model: str | None = None,
    provider: Provider | None = None,
    on_event: EventFn | None = None,
    max_calls: int | None = None,
    resume: bool = False,
    run_id: str | None = None,
) -> BenchResult:
    """Blind pairwise-bench the manuscript against `comp`, chapter-aligned."""
    cfg = project.config.readers
    load_comp(project, comp)  # raises if the comp is unknown
    personas = load_personas(project)
    roster_ids = list(roster) if roster else select_roster(personas, cfg.roster, cfg.roster_size)
    roster_ids = [pid for pid in roster_ids if pid in personas]
    if not roster_ids:
        raise ValueError("empty roster: no personas resolved")

    manuscript_nums = [c.number for c in project.chapters()]
    if chapters is not None:
        manuscript_nums = [n for n in manuscript_nums if n in set(chapters)]
    comp_bodies = load_comp_chapters(project, comp)
    aligned = min(len(manuscript_nums), len(comp_bodies))
    if aligned == 0:
        raise ValueError("nothing to bench: manuscript or comp has no chapters")
    aligned_nums = manuscript_nums[:aligned]

    cap = max_calls if max_calls is not None else cfg.max_calls_per_run

    # resume or fresh
    state: RunState | None = None
    if resume:
        target = run_id
        if target is None:
            from .state import list_runs

            prior = [s for s in list_runs(project) if s.kind == "bench" and s.comp_slug == comp]
            target = prior[0].run_id if prior else None
        if target is not None and state_path(project, target).exists():
            state = load_state(project, target)
    if state is None:
        state = RunState(
            run_id=run_id or new_run_id("bench"),
            kind="bench",
            chapters=aligned_nums,
            roster=roster_ids,
            comp_slug=comp,
        )
    state.budget.max_calls = cap

    roster_plan = state.roster or roster_ids
    chapters_plan = state.chapters or aligned_nums
    _plan_blind_map(state, chapters_plan)
    save_state(project, state)  # blind map persisted BEFORE any call

    ledger = Ledger(project.root)
    ledger.append("readers.bench.start", target=state.run_id, comp=comp, chapters=len(chapters_plan))

    result = BenchResult(
        run_id=state.run_id,
        comp=comp,
        chapters_aligned=aligned,
        comp_shorter=len(comp_bodies) < len(manuscript_nums),
        state_path=str(state_path(project, state.run_id)),
    )
    if result.comp_shorter:
        result.notes.append(
            f"comp {comp!r} has {len(comp_bodies)} chapters vs {len(manuscript_nums)} in the "
            f"manuscript; benched the first {aligned} aligned chapter(s) only"
        )

    batch_personas = [[personas[pid] for pid in b] for b in _chunk(roster_plan, cfg.personas_per_call)]

    while state.cursor < len(chapters_plan):
        if state.budget.calls_made >= cap:
            result.stopped_at_cap = True
            break
        idx = state.cursor
        chapter = chapters_plan[idx]
        try:
            _, manuscript_body = project.read_chapter(chapter)
        except ProjectError:
            result.notes.append(f"chapter {chapter} unreadable; skipped")
            state.cursor += 1
            save_state(project, state)
            continue
        comp_body = comp_bodies[idx]
        manuscript_slot = state.blind_map[chapter]
        text_a = manuscript_body if manuscript_slot == "a" else comp_body
        text_b = comp_body if manuscript_slot == "a" else manuscript_body

        tally = {"manuscript": 0, "comp": 0, "draws": 0, "total": 0}
        aborted = False
        for batch in batch_personas:
            if state.budget.calls_made >= cap:
                aborted = True
                result.stopped_at_cap = True
                break
            user = _render_bench_prompt(chapter, text_a, text_b, batch, state)
            state.budget.calls_made += 1
            save_state(project, state)  # BEFORE the call
            parsed, usage = batch_completion(
                project, _BENCH_SYSTEM, user, model=model, provider=provider
            )
            state.usage = state.usage + usage
            result.usage = result.usage + usage
            result.calls += 1
            ledger.append(
                "readers.bench.call",
                target=state.run_id,
                chapter=chapter,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            _tally_batch(parsed, batch, manuscript_slot, tally)

        if aborted:
            save_state(project, state)
            break

        state.bench_picks[chapter] = tally
        state.cursor += 1
        save_state(project, state)
        _emit(on_event, {"type": "bench.chapter", "n": chapter, **tally})

    _aggregate(project, state, result)
    ledger.append(
        "readers.bench.done",
        target=state.run_id,
        comp=comp,
        win_rate=round(result.manuscript_win_rate, 3),
        aggregation=result.aggregation,
    )
    save_state(project, state)
    return result


def _render_bench_prompt(
    chapter: int, text_a: str, text_b: str, batch: list[Persona], state: RunState
) -> str:
    from ..pipelines.common import render_prompt as _render_prompt

    states = {pid: state.ensure_reader(pid) for pid in (p.id for p in batch)}
    return _render_prompt(
        "readers_bench.md",
        {
            "chapter_number": f"{chapter:02d}",
            "text_a": text_a,
            "text_b": text_b,
            "readers_block": render_reader_block(batch, states),
        },
    )


def _tally_batch(
    parsed: dict[str, Any], batch: list[Persona], manuscript_slot: str, tally: dict[str, int]
) -> None:
    readers = parsed.get("readers") if isinstance(parsed, dict) else None
    if not isinstance(readers, dict):
        return
    for p in batch:
        entry = readers.get(p.id)
        if not isinstance(entry, dict):
            continue
        pick = str(entry.get("pick", "")).strip().lower()
        if pick not in ("a", "b"):
            continue
        tally["total"] += 1
        if pick == manuscript_slot:
            tally["manuscript"] += 1
        else:
            tally["comp"] += 1


def _aggregate(project: WritingProject, state: RunState, result: BenchResult) -> None:
    """Fold per-chapter pick tallies into win rates (and Elo when available)."""
    per_chapter: list[dict[str, Any]] = []
    total_m = total_c = 0
    for chapter in sorted(state.bench_picks):
        t = state.bench_picks[chapter]
        m, c = t["manuscript"], t["comp"]
        total_m += m
        total_c += c
        decisive = m + c
        per_chapter.append(
            {
                "chapter": chapter,
                "manuscript": m,
                "comp": c,
                "draws": t.get("draws", 0),
                "total": t["total"],
                "win_rate": round(m / decisive, 3) if decisive else 0.0,
            }
        )
    result.per_chapter = per_chapter
    decisive_total = total_m + total_c
    result.manuscript_win_rate = round(total_m / decisive_total, 3) if decisive_total else 0.0

    rating = _import_rating()
    if rating is None:
        result.aggregation = "win-rate"
        result.notes.append("tournament rating utilities unavailable; using win-rate aggregation")
    else:
        # Pool every decisive pick as one manuscript-vs-comp game and run Elo.
        rm = rc = rating.INITIAL_RATING
        for _ in range(total_m):
            rm, rc = rating.update(rm, rc, rating.WIN)
        for _ in range(total_c):
            rm, rc = rating.update(rm, rc, rating.LOSS)
        result.aggregation = "elo"
        result.ratings = {"manuscript": round(rm, 1), "comp": round(rc, 1)}

    _write_bench_json(project, state, result)


def _write_bench_json(project: WritingProject, state: RunState, result: BenchResult) -> None:
    rdir = run_dir(project, state.run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "bench",
        "run_id": state.run_id,
        "comp": result.comp,
        "chapters_aligned": result.chapters_aligned,
        "comp_shorter": result.comp_shorter,
        "aggregation": result.aggregation,
        "ratings": result.ratings,
        "manuscript_win_rate": result.manuscript_win_rate,
        "per_chapter": result.per_chapter,
        "blind_map": {str(k): v for k, v in state.blind_map.items()},
        "notes": result.notes,
    }
    (rdir / "bench.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
