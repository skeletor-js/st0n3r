"""Taste profile: the writer's blind A/B votes over `.stoner/taste.json`.

Plain JSON, counting stats, no ML. Votes reweight future tournaments two
ways -- a rendered digest injected into the judge prompt as an advisory
prior, and deterministic angle-selection weights -- but they never touch a
live tournament's Elo arithmetic: human data must not be laundered into
fake objectivity (invariants 1 and 2). Corruption handling mirrors
`tournament/state.py` (`.bak` + fresh profile).
"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ..ledger import Ledger
from ..project import WritingProject
from .state import TournamentState, save_state

MIN_VOTES_FOR_DIGEST = 5
_DIGEST_MAX_CHARS = 600


class TasteVote(BaseModel):
    """One blind vote on a pair of takes."""

    tournament_id: str
    chapter: int
    take_a: int
    take_b: int
    angle_a: str = ""
    angle_b: str = ""
    picked: int  # winning take index (the human's blind pick)
    judge_model: str = ""
    judge_pick: int | None = None  # judged winner of this pair, if decisive
    ts: float = Field(default_factory=time.time)


class TasteProfile(BaseModel):
    votes: list[TasteVote] = Field(default_factory=list)
    # Derived, recomputed on every append (counting only, no ML).
    angle_stats: dict[str, dict[str, int]] = Field(default_factory=dict)
    judge_agreement: dict[str, dict[str, int]] = Field(default_factory=dict)


def taste_path(project: WritingProject) -> Path:
    return project.root / ".stoner" / "taste.json"


def load_taste(project: WritingProject) -> TasteProfile:
    """Load the taste profile, tolerating corruption (`.bak` + fresh)."""
    p = taste_path(project)
    if not p.exists():
        return TasteProfile()
    try:
        return TasteProfile.model_validate_json(p.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        backup = p.parent / (p.name + ".bak")
        try:
            p.replace(backup)
        except OSError:
            pass
        return TasteProfile()


def save_taste(project: WritingProject, profile: TasteProfile) -> Path:
    p = taste_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return p


def _recompute_stats(profile: TasteProfile) -> None:
    angle_stats: dict[str, dict[str, int]] = {}
    agreement: dict[str, dict[str, int]] = {}
    for v in profile.votes:
        picked_angle = v.angle_a if v.picked == v.take_a else v.angle_b
        other_angle = v.angle_b if v.picked == v.take_a else v.angle_a
        if picked_angle:
            angle_stats.setdefault(picked_angle, {"wins": 0, "losses": 0})["wins"] += 1
        if other_angle:
            angle_stats.setdefault(other_angle, {"wins": 0, "losses": 0})["losses"] += 1
        if v.judge_model and v.judge_pick is not None:
            bucket = agreement.setdefault(v.judge_model, {"agree": 0, "disagree": 0})
            bucket["agree" if v.judge_pick == v.picked else "disagree"] += 1
    profile.angle_stats = angle_stats
    profile.judge_agreement = agreement


def record_vote(
    project: WritingProject,
    state: TournamentState,
    pair: tuple[int, int],
    picked: int,
) -> TasteVote:
    """Record one blind vote: append to the taste profile (recomputing the
    derived stats), mirror it onto the tournament state's vote list, and
    ledger `tournament.vote`. Cross-references the judge's verdict on the
    same pair (when judged and decisive) for the agreement stats. Live
    tournament standings are never modified."""
    ia, ib = pair
    if picked not in (ia, ib):
        raise ValueError(f"picked take {picked} is not in pair ({ia}, {ib})")
    rec_a, rec_b = state.take(ia), state.take(ib)
    if rec_a is None or rec_b is None:
        raise ValueError(f"unknown take in pair ({ia}, {ib})")

    judge_pick: int | None = None
    for c in state.comparisons:
        if frozenset((c.a, c.b)) == frozenset((ia, ib)):
            if c.verdict == "a":
                judge_pick = c.a
            elif c.verdict == "b":
                judge_pick = c.b
            break

    vote = TasteVote(
        tournament_id=state.id,
        chapter=state.chapter,
        take_a=ia,
        take_b=ib,
        angle_a=rec_a.angle,
        angle_b=rec_b.angle,
        picked=picked,
        judge_model=project.config.models.reviewer,
        judge_pick=judge_pick,
    )
    profile = load_taste(project)
    profile.votes.append(vote)
    _recompute_stats(profile)
    save_taste(project, profile)

    state.votes.append(vote.model_dump(mode="json"))
    save_state(project, state)
    Ledger(project.root).append(
        "tournament.vote",
        target=project.chapter_rel(state.chapter),
        tournament=state.id,
        pair=[ia, ib],
        picked=picked,
    )
    return vote


def angle_weights(profile: TasteProfile) -> dict[str, float]:
    """Human win rate per angle (angles with at least one vote outcome).
    Empty until `MIN_VOTES_FOR_DIGEST` votes exist, so selection order stays
    stable while the sample is tiny."""
    if len(profile.votes) < MIN_VOTES_FOR_DIGEST:
        return {}
    out: dict[str, float] = {}
    for angle, stats in profile.angle_stats.items():
        total = stats.get("wins", 0) + stats.get("losses", 0)
        if total > 0:
            out[angle] = stats.get("wins", 0) / total
    return out


def digest(profile: TasteProfile, max_chars: int = _DIGEST_MAX_CHARS) -> str:
    """Advisory prior for the judge prompt's `{taste_digest}` slot.

    Empty below `MIN_VOTES_FOR_DIGEST` votes. Never numeric-scores takes;
    it reports the writer's revealed preferences as win/loss counts."""
    if len(profile.votes) < MIN_VOTES_FOR_DIGEST:
        return ""
    parts = []
    ranked = sorted(
        profile.angle_stats.items(),
        key=lambda kv: (-(kv[1]["wins"] - kv[1]["losses"]), kv[0]),
    )
    angle_bits = [f"{name} {s['wins']}-{s['losses']}" for name, s in ranked]
    parts.append(
        f"Advisory prior -- the writer's blind votes ({len(profile.votes)} so far) "
        f"favor these angles: {', '.join(angle_bits)}."
    )
    for _model_str, s in sorted(profile.judge_agreement.items()):
        total = s["agree"] + s["disagree"]
        if total:
            pct = round(100 * s["agree"] / total)
            parts.append(
                f"This judge model agreed with the writer on {pct}% of {total} past votes."
            )
    text = " ".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text
