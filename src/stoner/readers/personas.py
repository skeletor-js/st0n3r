"""Persona library and loader for reader simulation.

The package ships a starter roster of distinct reader personas as a single flat
YAML file under `data/personas.yaml`; projects extend or override them with
hand-authored files under `.stoner/readers/personas/*.yaml`, merged by id with
project definitions winning outright.

The loader mirrors `slop/lexicon.py`: a module-level `DATA_DIR`, a cached load
of the shipped data, and a `force_reload` hook for tests that monkeypatch the
data dir. A `Persona.voice_block()` render is used *verbatim* in the batched
chapter prompt, so a persona is defined by concrete, differentiated taste and
voice, not by a numeric knob -- distinctness across age, genre prior, and
patience is what makes the disagreement analysis meaningful (a roster whose
voices collapse into one reader produces a suspiciously agreement-heavy
heatmap).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from ..project import WritingProject

DATA_DIR = Path(__file__).parent / "data"

Patience = Literal["low", "medium", "high"]

# Required scalar/list fields a persona mapping must supply. `quirks` and
# `voice_notes` are optional (they default), everything else is load-bearing
# for the voice block and the attribute splits.
_REQUIRED_FIELDS = ("id", "name", "age", "patience", "genre_priors", "tastes")


class PersonaError(ValueError):
    """Raised when a persona YAML file is malformed or ids collide."""


class Persona(BaseModel):
    """One reader persona. `id` is stable; everything else feeds the prompt."""

    id: str
    name: str
    age: int
    patience: Patience
    genre_priors: list[str] = Field(default_factory=list)  # ordered, first is primary
    tastes: list[str] = Field(default_factory=list)  # concrete likes/dislikes
    quirks: list[str] = Field(default_factory=list)
    voice_notes: str = ""

    @property
    def primary_genre(self) -> str:
        return self.genre_priors[0] if self.genre_priors else "unspecified"

    @property
    def age_band(self) -> str:
        return age_band(self.age)

    def voice_block(self) -> str:
        """The verbatim persona block dropped into a chapter prompt."""
        lines = [
            f"### {self.name} (id: {self.id})",
            f"Age {self.age} ({self.age_band}). Patience: {self.patience}. "
            f"Reads mostly {', '.join(self.genre_priors) or 'anything'}.",
            f"Tastes: {'; '.join(self.tastes) or 'no strong preferences'}.",
        ]
        if self.quirks:
            lines.append(f"Quirks: {'; '.join(self.quirks)}.")
        if self.voice_notes:
            lines.append(f"Voice: {self.voice_notes}")
        return "\n".join(lines)


def age_band(age: int) -> str:
    """Coarse age band used for disagreement splits and roster spread."""
    if age < 20:
        return "teen"
    if age < 30:
        return "20s"
    if age < 40:
        return "30s"
    if age < 50:
        return "40s"
    if age < 60:
        return "50s"
    return "60+"


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def _load_yaml_list(path: Path) -> list[dict[str, Any]]:
    """Read a persona YAML file as a list of mappings. A single top-level
    mapping is tolerated (wrapped in a one-element list) so a project file may
    hold one persona."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise PersonaError(f"{path}: expected a YAML list of persona mappings")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise PersonaError(f"{path}: non-mapping persona entry: {item!r}")
        out.append(item)
    return out


def _parse_persona(item: dict[str, Any], *, where: str) -> Persona:
    for field in _REQUIRED_FIELDS:
        if field not in item or item[field] in (None, "", []):
            raise PersonaError(f"{where}: persona is missing required field {field!r}")
    try:
        return Persona.model_validate(item)
    except ValidationError as exc:
        # Surface the first offending field by name for an actionable message.
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ())) or "?"
        raise PersonaError(f"{where}: invalid persona field {loc!r}: {first.get('msg')}") from exc


def _merge_file(
    into: dict[str, Persona], path: Path, *, allow_override: bool
) -> None:
    """Parse `path` and merge its personas into `into`. Duplicate ids *within*
    a single file always raise; `allow_override` governs cross-file overrides
    (project files override shipped personas by id)."""
    seen_here: set[str] = set()
    for item in _load_yaml_list(path):
        pid = str(item.get("id", "")).strip()
        where = f"{path.name}: persona {pid!r}" if pid else path.name
        persona = _parse_persona(item, where=where)
        if persona.id in seen_here:
            raise PersonaError(f"{path}: duplicate persona id {persona.id!r} in the same file")
        seen_here.add(persona.id)
        if persona.id in into and not allow_override:
            raise PersonaError(
                f"{path}: duplicate persona id {persona.id!r} across shipped data files"
            )
        into[persona.id] = persona


# ---------------------------------------------------------------------------
# Shipped library (cached) + project overlay
# ---------------------------------------------------------------------------

_cache: dict[str, Persona] | None = None


def load_shipped(*, force_reload: bool = False) -> dict[str, Persona]:
    """Load (and cache) the packaged persona library, keyed by id."""
    global _cache
    if _cache is None or force_reload:
        merged: dict[str, Persona] = {}
        for path in sorted(DATA_DIR.glob("*.yaml")):
            _merge_file(merged, path, allow_override=False)
        if len(merged) < 20:
            raise PersonaError(
                f"shipped persona library has only {len(merged)} personas (expected >= 20)"
            )
        _cache = merged
    return _cache


def _project_personas_dir(project: WritingProject) -> Path:
    return project.root / ".stoner" / "readers" / "personas"


def load_personas(
    project: WritingProject | None = None, *, force_reload: bool = False
) -> dict[str, Persona]:
    """Shipped personas overlaid with project personas (project wins by id).

    Project personas live under `.stoner/readers/personas/*.yaml`; each file is
    a YAML list of persona mappings (or a single mapping). A malformed file or
    an id that collides *within* one file raises `PersonaError` naming the file.
    """
    personas = dict(load_shipped(force_reload=force_reload))
    if project is None:
        return personas
    pdir = _project_personas_dir(project)
    if not pdir.exists():
        return personas
    for path in sorted(pdir.glob("*.yaml")):
        _merge_file(personas, path, allow_override=True)
    return personas


# ---------------------------------------------------------------------------
# Roster selection
# ---------------------------------------------------------------------------


def default_roster(personas: dict[str, Persona], size: int) -> list[str]:
    """A deterministic roster of up to `size` ids maximizing attribute spread.

    Greedy: repeatedly pick the persona (in id order for ties) that introduces
    the most not-yet-seen values across the (age band, patience, primary genre)
    axes. Once every value is represented the remaining picks fall back to id
    order, so the result is stable and reproducible.
    """
    if size <= 0:
        return []
    remaining = sorted(personas.values(), key=lambda p: p.id)
    seen: dict[str, set[str]] = {"age": set(), "patience": set(), "genre": set()}
    chosen: list[str] = []
    while remaining and len(chosen) < size:
        def novelty(p: Persona) -> int:
            score = 0
            if p.age_band not in seen["age"]:
                score += 1
            if p.patience not in seen["patience"]:
                score += 1
            if p.primary_genre not in seen["genre"]:
                score += 1
            return score

        best = max(remaining, key=lambda p: (novelty(p), -_neg_id_rank(p, remaining)))
        chosen.append(best.id)
        seen["age"].add(best.age_band)
        seen["patience"].add(best.patience)
        seen["genre"].add(best.primary_genre)
        remaining.remove(best)
    return chosen


def _neg_id_rank(p: Persona, pool: list[Persona]) -> int:
    # Tie-break helper: lower id sorts first. `pool` is already id-sorted, so
    # the index is a stable lexicographic rank.
    return pool.index(p)


def select_roster(
    personas: dict[str, Persona], explicit_ids: list[str], roster_size: int
) -> list[str]:
    """Resolve a roster: explicit config ids (validated) else the default
    spread roster of `roster_size`."""
    if explicit_ids:
        unknown = [i for i in explicit_ids if i not in personas]
        if unknown:
            raise PersonaError(
                f"roster references unknown persona id(s): {', '.join(sorted(unknown))}"
            )
        return list(explicit_ids)
    return default_roster(personas, roster_size)
