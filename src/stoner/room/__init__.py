"""The Writers' Room: a persistent editorial room over `review/passes.py`.

Named editors (developmental editor, line editor, continuity pedant, first
reader) keep notebooks across the whole project, re-check whether prior flags
were addressed, disagree with each other on the record, and answer the
writer's margin comments. Editors *wrap* the existing `ReviewPass` machinery
at the (system, user) tuple level -- nothing here changes `review/passes.py`,
`PassContext`, or `types.py`, and nothing about the room gates.

Submodules:
  - `roster`    -- editor slugs, persona defaults, pass-assignment validation
  - `notebook`  -- persistent, capped, per-editor memory (`.stoner/room/notebooks/`)
  - `comments`  -- margin comments pinned to spans (`.stoner/room/comments/`)
  - `relocate`  -- deterministic-first re-location of prior flags, one LLM fallback
  - `session`   -- the end-to-end session engine (`.stoner/room/sessions/`)
"""

from __future__ import annotations

from .roster import Editor, load_roster, slugify

__all__ = ["Editor", "load_roster", "slugify"]
