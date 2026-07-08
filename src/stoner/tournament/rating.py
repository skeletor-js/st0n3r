"""Pure Elo arithmetic and pairing schedules. No model calls, no I/O.

Ratings are deterministic arithmetic over pairwise verdicts only (the
milestone-1 research finding: comparative judgments spread out where
absolute LLM scores collapse). Fixed K, fixed initial rating, draws score
0.5. Pairing is full round-robin for small fields and Swiss-style (adjacent
standings, no rematches) for larger ones.
"""

from __future__ import annotations

import math

K_FACTOR = 32.0
INITIAL_RATING = 1200.0

# Outcome values from the perspective of the first take in a pair.
WIN = 1.0
LOSS = 0.0
DRAW = 0.5


def expected(ra: float, rb: float) -> float:
    """Expected score of the `ra` side against `rb` (standard logistic)."""
    return 1.0 / (1.0 + 10.0 ** ((rb - ra) / 400.0))


def update(ra: float, rb: float, outcome: float) -> tuple[float, float]:
    """New (ra, rb) after one game; `outcome` is the first side's score
    (1 win, 0 loss, 0.5 draw)."""
    ea = expected(ra, rb)
    delta = K_FACTOR * (outcome - ea)
    return ra + delta, rb - delta


def round_robin_pairs(indices: list[int]) -> list[tuple[int, int]]:
    """Every unordered pair, in deterministic index order."""
    ordered = sorted(indices)
    return [
        (ordered[i], ordered[j])
        for i in range(len(ordered))
        for j in range(i + 1, len(ordered))
    ]


def swiss_pairs(
    ratings: dict[int, float], played: set[frozenset[int]]
) -> list[tuple[int, int]]:
    """One Swiss round: sort by rating (desc, index tiebreak) and pair each
    take with the nearest lower-standing take it has not already played,
    backtracking so no take sits out when a legal full pairing exists.
    Deterministic (fields are tiny, n <= 8 in practice); with no legal
    opponent left a take sits the round out.
    """
    standing = sorted(ratings, key=lambda i: (-ratings[i], i))
    full = len(standing) // 2
    best: list[tuple[int, int]] = []

    def dfs(remaining: tuple[int, ...], acc: list[tuple[int, int]]) -> None:
        nonlocal best
        if len(acc) + len(remaining) // 2 <= len(best) and best:
            return  # cannot beat the best pairing found so far
        if len(remaining) < 2:
            if len(acc) > len(best):
                best = list(acc)
            return
        a, rest = remaining[0], remaining[1:]
        for idx, b in enumerate(rest):
            if frozenset((a, b)) in played:
                continue
            dfs(rest[:idx] + rest[idx + 1 :], acc + [(a, b)])
            if len(best) == full:
                return  # first full pairing wins (adjacent-first preference)
        dfs(rest, acc)  # a sits this round out

    dfs(tuple(standing), [])
    return best


def swiss_rounds(n: int) -> int:
    """ceil(log2 n) + 1 rounds: adequate ranking confidence for n <= 8
    given both-orders judging."""
    if n < 2:
        return 0
    return math.ceil(math.log2(n)) + 1
