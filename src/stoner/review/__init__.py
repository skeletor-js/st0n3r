"""The review engine: critic passes, the pass runner, and revision.

Public surface (see `docs/planning/ARCHITECTURE.md` "Review engine"):

- `run_review(project, chapter, passes=None, model=None, provider=None)` ->
  `ReviewReport`, saved under `.stoner/reviews/`.
- `revise_chapter(project, chapter, findings, model=None, provider=None)` ->
  `ReviseResult`, rewriting the chapter in place.
- `PASSES` -- the built-in critic pass registry, for callers (CLI/UI) that
  want to list or select passes by name.
"""

from __future__ import annotations

from .passes import PASSES, PassContext, ReviewPass, build_context, extract_json, locate_span
from .revise import ReviseResult, revise_chapter
from .runner import run_review

__all__ = [
    "PASSES",
    "PassContext",
    "ReviewPass",
    "ReviseResult",
    "build_context",
    "extract_json",
    "locate_span",
    "revise_chapter",
    "run_review",
]
