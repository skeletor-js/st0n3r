"""Drafting angles: preset table, config merge, and deterministic selection.

An angle is data (name + drafting instruction), layered onto the existing
`writer.md` system prompt as a task augmentation -- one writer prompt stays
the single source of drafting truth. Projects add angles via the
`tournament.angles` config list; taste weighting (from blind human votes)
reorders selection deterministically, never randomly.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import TournamentConfig
from ..project import WritingProject


@dataclass(frozen=True)
class Angle:
    name: str
    instruction: str


PRESET_ANGLES: list[Angle] = [
    Angle(
        "in_scene",
        "Stay inside the scene from the first line: live action, present "
        "consequence, no retrospection. The chapter's events happen on the "
        "page, in real time, moment by moment.",
    ),
    Angle(
        "aftermath",
        "Open after the chapter's central event has already happened and "
        "let the reader reconstruct it from wreckage, reaction, and what "
        "characters can't say. The event itself stays offstage.",
    ),
    Angle(
        "pov_tight",
        "Lock the camera inside the POV character's skull: only what they "
        "perceive, misread, or refuse to notice. No omniscient framing, no "
        "information the POV character doesn't have.",
    ),
    Angle(
        "pov_distant",
        "Pull the camera back: a cooler, more distant narration that "
        "watches the characters from outside and lets gesture and setting "
        "carry the feeling. Interior access is rationed to a few decisive "
        "moments.",
    ),
    Angle(
        "dialogue_led",
        "Let talk do the work: build the chapter around two or three "
        "extended exchanges where subtext, evasion, and interruption carry "
        "the plot. Narration is connective tissue, not the engine.",
    ),
    Angle(
        "interior_led",
        "Build the chapter on interiority: thought, memory, and the gap "
        "between what the POV character feels and what they do. Dialogue is "
        "sparse and load-bearing when it comes.",
    ),
    Angle(
        "late_entry",
        "Enter every scene as late as possible and leave early. Cut the "
        "arrivals, greetings, and wind-downs; start where the pressure is "
        "already on and trust the reader to catch up.",
    ),
    Angle(
        "image_first",
        "Anchor the chapter to one concrete recurring image or object. "
        "Open on it, return to it under changed light, and let it carry the "
        "chapter's meaning instead of stating the theme.",
    ),
]


def all_angles(config: TournamentConfig) -> list[Angle]:
    """Presets plus project-defined angles; a config angle with a preset's
    name replaces the preset (project definition wins)."""
    by_name = {a.name: a for a in PRESET_ANGLES}
    order = [a.name for a in PRESET_ANGLES]
    for entry in config.angles:
        name = str(entry.get("name", "")).strip()
        instruction = str(entry.get("instruction", "")).strip()
        if not name or not instruction:
            continue
        if name not in by_name:
            order.append(name)
        by_name[name] = Angle(name, instruction)
    return [by_name[n] for n in order]


def select_angles(
    n: int, config: TournamentConfig, weights: dict[str, float] | None = None
) -> list[Angle]:
    """Pick `n` distinct angles deterministically.

    Default order is preset order (then config order). When `weights` (human
    win rates from the taste profile) is provided, angles sort by weight
    descending with ties broken by default order -- deterministic, never
    random. If `n` exceeds the available angles, the list cycles (angles may
    repeat rather than failing the run).
    """
    pool = all_angles(config)
    if not pool:
        raise ValueError("no drafting angles available")
    if weights:
        default_pos = {a.name: i for i, a in enumerate(pool)}
        pool = sorted(pool, key=lambda a: (-weights.get(a.name, 0.5), default_pos[a.name]))
    return [pool[i % len(pool)] for i in range(n)]


def slot_for_chapter(project: WritingProject, chapter: int) -> str | None:
    """`opening` for ch-01, `ending` for the last planned chapter, else None."""
    if chapter == 1:
        return "opening"
    from ..pipelines.book import planned_chapters  # late: avoids import cycle risk

    planned = planned_chapters(project)
    if planned and chapter == max(planned):
        return "ending"
    return None


def takes_for_chapter(project: WritingProject, chapter: int, override: int | None = None) -> int:
    """Field size: explicit override > per-slot config > default takes."""
    if override is not None:
        if override < 2:
            raise ValueError("a tournament needs at least 2 takes")
        return override
    cfg = project.config.tournament
    slot = slot_for_chapter(project, chapter)
    if slot and slot in cfg.slot_takes:
        return max(2, int(cfg.slot_takes[slot]))
    return max(2, cfg.takes)
