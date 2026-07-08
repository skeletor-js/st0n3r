"""The Promise & Motif Ledger's measurement half: deterministic motif scans
and their advisory LLM judgments.

Public API:

- Deterministic (offline, reproducible): :func:`scan_motifs` (recurrence
  matrix), :func:`mine_candidates` (unregistered recurring n-grams),
  :func:`rhyme_overlap` (opening/closing overlap). All in :mod:`scan`.
- Advisory (LLM, categorical): :func:`judge_candidates`, :func:`judge_rhyme`
  in :mod:`judge` -- both emit `Finding`s and never write canon.
- Rendering + saving: :func:`render`, :func:`save_scan`, :func:`save_rhyme`
  in :mod:`report`.

Promises live in `canon/threads.md` as typed rows (see `canon/store.py`), so
they have no module here; this package is motifs + rhyme only. See
`docs/plans/2026-07-07-007-feat-promise-motif-ledger-plan.md`.
"""

from __future__ import annotations

from .judge import judge_candidates, judge_rhyme
from .report import render, save_rhyme, save_scan
from .scan import (
    CandidateReport,
    CandidateRow,
    MotifScanReport,
    MotifScanRow,
    RhymeReport,
    mine_candidates,
    rhyme_overlap,
    scan_motifs,
)

__all__ = [
    "scan_motifs",
    "mine_candidates",
    "rhyme_overlap",
    "judge_candidates",
    "judge_rhyme",
    "render",
    "save_scan",
    "save_rhyme",
    "MotifScanReport",
    "MotifScanRow",
    "CandidateReport",
    "CandidateRow",
    "RhymeReport",
]
