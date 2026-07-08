"""Draft tournaments: draft N takes of a chapter from distinct angles, judge
them in blind pairwise comparisons with Elo standings, and let the human
confirm (and optionally graft) the winner.

Public seam for pipeline integration (plan 011): `run_tournament` and
`apply_winner` in `stoner.tournament.run`.
"""

from .run import TournamentResult, apply_winner, run_tournament

__all__ = ["TournamentResult", "apply_winner", "run_tournament"]
