"""Blind pairwise judging: both presentation orders, STRICT JSON verdicts,
deterministic Elo updates, budget enforcement, and the winner proposal.

Hard invariants honored here:
- Judging is A/B only. No numeric scores appear in the prompt or the parse;
  standings are Elo arithmetic over verdicts (rating.py).
- Position bias is converted to draws: each pair is judged twice with the
  presentation order swapped, and the two verdicts must agree to count as a
  win -- disagreement records a draw.
- Blind presentation: the judge sees take bodies only (no angle names, no
  slop scores, anonymous A/B labels).
- State is saved BEFORE every model call; an unparseable verdict is retried
  once and then the pair records a draw with a note -- degradation never
  aborts a tournament (text-only providers included).
- The proposal is advisory: this module sets `status=proposed` and never
  touches the manuscript.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..ledger import Ledger
from ..project import WritingProject
from ..providers.base import Provider
from ..review.passes import extract_json
from ..types import Usage
from .rating import (
    DRAW,
    INITIAL_RATING,
    LOSS,
    WIN,
    round_robin_pairs,
    swiss_pairs,
    swiss_rounds,
    update,
)
from .state import Comparison, TournamentState, save_state
from .takes import read_take_body

EventFn = Callable[[dict[str, Any]], None]

_ROUND_ROBIN_MAX = 4
_JUDGE_SYSTEM = (
    "You judge competing drafts of a single book chapter, presented blind. "
    "You answer with STRICT JSON only: a forced A/B verdict, one named "
    "steal from the loser, and a one-sentence why. Never use numeric "
    "scores, ratings, or grades."
)


def _emit(on_event: EventFn | None, event: dict[str, Any]) -> None:
    if on_event is not None:
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - a broken UI callback must not abort the run
            pass


def _judge_context(project: WritingProject, chapter: int, taste_digest: str) -> dict[str, str]:
    def read_or_empty(rel: str) -> str:
        try:
            return project.read(rel)
        except Exception:  # noqa: BLE001 - missing context files render as ""
            return ""

    return {
        "project_name": project.config.project_name,
        "chapter_number": f"{chapter:02d}",
        "premise": read_or_empty("canon/premise.md"),
        "beats": read_or_empty(f"outline/beats/ch-{chapter:02d}.md"),
        "taste_digest": taste_digest,
    }


def _parse_verdict(text: str) -> dict[str, Any] | None:
    """Verdict dict with winner "A"/"B", or None when unparseable."""
    data = extract_json(text)
    winner = str(data.get("winner", "")).strip().upper()
    if winner not in ("A", "B"):
        return None
    steal = data.get("steal")
    steal_from, steal_move = "", ""
    if isinstance(steal, dict):
        steal_from = str(steal.get("from", "")).strip().upper()
        steal_move = str(steal.get("move", "")).strip()
    return {"winner": winner, "steal_from": steal_from, "steal_move": steal_move}


def _one_order(
    project: WritingProject,
    state: TournamentState,
    user: str,
    model: str | None,
    provider: Provider | None,
) -> tuple[dict[str, Any] | None, Usage]:
    """One judge call (with one retry on unparseable JSON). Saves state
    before each call and counts each call against the comparison budget."""
    from ..pipelines.common import call_model

    usage = Usage()
    verdict: dict[str, Any] | None = None
    for _attempt in range(2):
        save_state(project, state)
        text, u = call_model(
            project, "reviewer", system=_JUDGE_SYSTEM, user=user, model=model, provider=provider
        )
        usage += u
        state.comparisons_done += 1
        state.tokens_used += u.input_tokens + u.output_tokens
        verdict = _parse_verdict(text)
        if verdict is not None:
            break
    return verdict, usage


def judge_pair(
    project: WritingProject,
    state: TournamentState,
    ia: int,
    ib: int,
    taste_digest: str = "",
    model: str | None = None,
    provider: Provider | None = None,
) -> tuple[Comparison, Usage]:
    """Judge takes `ia` vs `ib` in both presentation orders.

    Agreement on the same take -> decisive verdict; disagreement (or an
    unparseable order after one retry) -> draw. Steals named by the judge
    accumulate on the take they were stolen from.
    """
    from ..pipelines.common import render_prompt

    usage = Usage()
    ctx = _judge_context(project, state.chapter, taste_digest)
    take_a_rec, take_b_rec = state.take(ia), state.take(ib)
    if take_a_rec is None or take_b_rec is None:
        raise ValueError(f"unknown take in pair ({ia}, {ib})")
    body_a = read_take_body(project, take_a_rec)
    body_b = read_take_body(project, take_b_rec)

    picks: list[int | None] = []  # winning take index per order, None = no-contest
    steals: list[tuple[int, str]] = []  # (take index stolen from, move)
    for first, second in ((ia, ib), (ib, ia)):
        bodies = {ia: body_a, ib: body_b}
        user = render_prompt(
            "tournament_judge.md",
            {**ctx, "take_a": bodies[first], "take_b": bodies[second]},
        )
        verdict, order_usage = _one_order(project, state, user, model, provider)
        usage += order_usage
        if verdict is None:
            picks.append(None)
            continue
        label_to_take = {"A": first, "B": second}
        picks.append(label_to_take[verdict["winner"]])
        if verdict["steal_move"] and verdict["steal_from"] in label_to_take:
            steals.append((label_to_take[verdict["steal_from"]], verdict["steal_move"]))

    note = ""
    if None in picks:
        result = "draw"
        note = "unparseable verdict after retry; recorded as draw"
    elif picks[0] == picks[1]:
        result = "a" if picks[0] == ia else "b"
    else:
        result = "draw"
        note = "orders disagreed; recorded as draw"

    # Steals land on the takes they were named from; on a decisive result
    # only the loser's steals matter, but keeping every named move costs
    # nothing and grafting filters to non-winners anyway.
    winner_take = ia if result == "a" else ib if result == "b" else None
    for take_idx, move in steals:
        if take_idx == winner_take:
            continue
        rec = state.take(take_idx)
        if rec is not None and move not in rec.steals:
            rec.steals.append(move)

    comparison = Comparison(a=ia, b=ib, verdict=result, note=note)  # type: ignore[arg-type]
    return comparison, usage


def run_judging(
    project: WritingProject,
    state: TournamentState,
    taste_digest: str = "",
    model: str | None = None,
    provider: Provider | None = None,
    on_event: EventFn | None = None,
) -> Usage:
    """Judge the pairing schedule, update Elo, and propose a winner.

    Round-robin for fields of <= 4 takes; Swiss (ceil(log2 N)+1 rounds,
    adjacent standings, no rematches) otherwise. Stops early when either
    budget trips -- standings stand and the proposal is made from current
    Elo with a note. Resumable: already-judged pairs are skipped.
    """
    usage = Usage()
    ledger = Ledger(project.root)
    state.status = "judging"
    indices = [t.index for t in state.takes]
    for i in indices:
        state.ratings.setdefault(i, INITIAL_RATING)
    save_state(project, state)

    def budget_left() -> bool:
        if state.comparisons_done + 2 > state.max_comparisons:
            note = f"comparison budget ({state.max_comparisons} calls) reached"
            if note not in state.notes:
                state.notes.append(note)
            return False
        if state.tokens_used >= state.max_tokens_budget:
            note = f"token budget ({state.max_tokens_budget}) reached"
            if note not in state.notes:
                state.notes.append(note)
            return False
        return True

    def judge_schedule(pairs: list[tuple[int, int]]) -> bool:
        """Judge pairs in order; False when a budget stopped the run."""
        nonlocal usage
        for ia, ib in pairs:
            if frozenset((ia, ib)) in state.played_pairs():
                continue
            if not budget_left():
                return False
            comparison, pair_usage = judge_pair(
                project,
                state,
                ia,
                ib,
                taste_digest=taste_digest,
                model=model,
                provider=provider,
            )
            usage += pair_usage
            state.comparisons.append(comparison)
            outcome = (
                WIN if comparison.verdict == "a" else LOSS if comparison.verdict == "b" else DRAW
            )
            state.ratings[ia], state.ratings[ib] = update(
                state.ratings[ia], state.ratings[ib], outcome
            )
            save_state(project, state)
            ledger.append(
                "tournament.compare",
                target=project.chapter_rel(state.chapter),
                tournament=state.id,
                pair=[ia, ib],
                verdict=comparison.verdict,
            )
            _emit(
                on_event,
                {"type": "pair.judged", "pair": (ia, ib), "verdict": comparison.verdict},
            )
        return True

    if len(indices) <= _ROUND_ROBIN_MAX:
        judge_schedule(round_robin_pairs(indices))
    else:
        for _round in range(swiss_rounds(len(indices))):
            pairs = swiss_pairs(state.ratings, state.played_pairs())
            if not pairs or not judge_schedule(pairs):
                break

    # Propose from current standings (tie-break: earlier take wins).
    state.proposed_winner = min(
        state.ratings, key=lambda i: (-state.ratings[i], i)
    )
    state.status = "proposed"
    save_state(project, state)
    ledger.append(
        "tournament.propose",
        target=project.chapter_rel(state.chapter),
        tournament=state.id,
        winner=state.proposed_winner,
        comparisons=len(state.comparisons),
    )
    _emit(on_event, {"type": "winner.proposed", "take": state.proposed_winner})
    return usage
