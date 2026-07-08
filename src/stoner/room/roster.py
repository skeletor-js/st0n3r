"""Roster: resolve `RoomConfig.editors` into runnable `Editor` handles.

An `Editor` pairs an `EditorSpec` (name, persona, assigned pass names) with a
filesystem slug and its *known* passes -- the subset of assigned passes that
actually exist in `review/passes.PASSES`. Unknown pass names are dropped with
a warning note rather than raising, mirroring the runner's unknown-pass
degradation (`runner._run_one_pass` returns an info Finding for an unknown
pass). This keeps a roster loadable even when it names a pass that was
renamed or belongs to a feature not yet installed.

Slugs are the filesystem identity of an editor: they name the notebook file
(`.stoner/room/notebooks/<slug>.json`) and appear in finding sources
(`room:<slug>:<pass>`), so they must be stable and collision-free within a
roster. `load_roster` disambiguates collisions deterministically by suffixing
`-2`, `-3`, ... in roster order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import EditorSpec, RoomConfig
from ..review.passes import PASSES

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Filesystem-safe slug for an editor name: lowercase, hyphen-joined."""
    slug = _SLUG_STRIP_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "editor"


@dataclass
class Editor:
    """A runnable editor: its spec, its slug, and its known pass names."""

    spec: EditorSpec
    slug: str
    passes: list[str] = field(default_factory=list)  # known passes only
    unknown_passes: list[str] = field(default_factory=list)  # dropped, for warnings

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def persona(self) -> str:
        return self.spec.persona


def load_roster(config: RoomConfig) -> list[Editor]:
    """Resolve `config.editors` into `Editor` handles with unique slugs.

    Validation is soft: a pass name not in `PASSES` is recorded in
    `unknown_passes` (surfaced as a warning by callers) and excluded from the
    editor's runnable `passes`, never raised.
    """
    editors: list[Editor] = []
    seen: dict[str, int] = {}
    for spec in config.editors:
        base = slugify(spec.name)
        count = seen.get(base, 0) + 1
        seen[base] = count
        slug = base if count == 1 else f"{base}-{count}"
        known = [p for p in spec.passes if p in PASSES]
        unknown = [p for p in spec.passes if p not in PASSES]
        editors.append(Editor(spec=spec, slug=slug, passes=known, unknown_passes=unknown))
    return editors
