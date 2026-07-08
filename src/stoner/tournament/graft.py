"""Graft: fold the losers' named steals into the confirmed winner.

One revise-style plain completion, guarded exactly like
`review/revise.py:revise_chapter`: sentinel extraction (BEGIN CHAPTER /
END CHAPTER), refusal when the result is empty or under 1/4 of the
original word count. A refused graft falls back to the raw winner with a
note -- the winning take is never destroyed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..canon.store import CanonStore
from ..project import WritingProject, count_words
from ..providers.base import Provider
from ..types import Usage

# Sentinel tolerance matches review/revise.py: fall back to "everything
# after BEGIN CHAPTER" when END is missing, and to the whole trimmed
# response when neither sentinel is present.
_BEGIN_RE = re.compile(r"BEGIN\s+CHAPTER\s*\n?", re.IGNORECASE)
_END_RE = re.compile(r"\n?\s*END\s+CHAPTER", re.IGNORECASE)

_GRAFT_SYSTEM = (
    "You are a meticulous line editor grafting specific strong moves from "
    "losing drafts onto the winning draft of a chapter. Work in each steal "
    "surgically, preserve the winner's voice and structure, and change "
    "nothing beyond what the steals require."
)


@dataclass
class GraftResult:
    """Outcome of one graft attempt."""

    body: str | None  # grafted body, or None when the graft was refused
    note: str = ""
    usage: Usage = field(default_factory=Usage)


def _extract_grafted_body(text: str) -> str:
    begin_m = _BEGIN_RE.search(text)
    if begin_m is None:
        return text.strip()
    end_m = _END_RE.search(text, begin_m.end())
    if end_m is not None and end_m.start() > begin_m.end():
        return text[begin_m.end() : end_m.start()].strip("\n")
    return text[begin_m.end() :].strip("\n")


def graft_winner(
    project: WritingProject,
    chapter: int,
    winner_body: str,
    steals: list[str],
    model: str | None = None,
    provider: Provider | None = None,
) -> GraftResult:
    """One `call_model` against the reviewer role folding `steals` into
    `winner_body`. Never raises on a bad model response: a truncated/empty
    graft returns `body=None` with a note so the caller applies the raw
    winner instead."""
    from ..pipelines.common import call_model, render_prompt

    if not steals:
        return GraftResult(body=None, note="no steals to graft")

    store = CanonStore(project)
    user = render_prompt(
        "tournament_graft.md",
        {
            "project_name": project.config.project_name,
            "chapter_number": f"{chapter:02d}",
            "winner_body": winner_body,
            "steals": "\n".join(f"- {s}" for s in steals),
            "style_guide": store.style_body_without_banned(),
        },
    )
    text, usage = call_model(
        project, "reviewer", system=_GRAFT_SYSTEM, user=user, model=model, provider=provider
    )

    grafted = _extract_grafted_body(text)
    old_words = count_words(winner_body)
    new_words = count_words(grafted)
    if new_words == 0 or new_words < old_words // 4:
        return GraftResult(
            body=None,
            note=(
                f"graft refused: response was {new_words} words "
                f"(winner: {old_words}); applying the raw winner"
            ),
            usage=usage,
        )
    return GraftResult(body=grafted, usage=usage)
