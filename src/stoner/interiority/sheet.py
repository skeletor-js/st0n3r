"""Cast-sheet pydantic models: a major character's private interior state.

These models hold exactly what the writer agent must never see -- what a
character knows (and *when* they learned it), what they want but won't say,
what they fear, the lies they are actively maintaining, and the topics they
refuse to discuss. They live in `.stoner/cast/<slug>.json`, outside `canon/`,
because canon is the writer's context source by construction
(`CanonStore.context_pack`); privacy here is structural, not prompt discipline
(see `docs/plans/2026-07-07-002-feat-character-interiority-agents-plan.md`).

The public face of a character -- appearance, role, voice -- stays in
`canon/characters/<slug>.md`. A sheet only carries what the narrator must not
know. Knowledge/lie ids are assigned by the store, never by a model.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

# how a character came to know a fact -- free-ish, but these are the values
# the curator prompt asks for; parsing is tolerant of anything.
KNOWLEDGE_HOW = ("witnessed", "told", "inferred", "backstory")


class Wants(BaseModel):
    """A character's stated want (what they say they're after) vs. their real
    want (the thing driving them that they will not name)."""

    stated: str = ""
    real: str = ""

    def is_set(self) -> bool:
        return bool(self.stated.strip() or self.real.strip())


class KnowledgeEntry(BaseModel):
    """One fact a character knows, keyed to the chapter they learned it in.

    `learned_in` is a chapter number; 0 means pre-story backstory. `secret`
    marks knowledge whose leak is the worst class of boundedness violation --
    a character acting on an unlearned secret escalates to a critical finding.
    """

    id: str  # store-assigned, per-sheet sequential: k001, k002, ...
    fact: str
    learned_in: int = 0
    how: str = "backstory"  # witnessed | told | inferred | backstory
    source: str = ""  # supporting quote or note
    secret: bool = False


class Lie(BaseModel):
    """An active deception: a claim the character makes that contradicts a
    truth they hold. `truth` may reference a knowledge-entry id or be free
    text. `exposed_in` records the chapter a lie was exposed (or None)."""

    id: str  # store-assigned, per-sheet sequential: l001, l002, ...
    claim: str
    truth: str = ""  # a knowledge id (e.g. "k004") or free text
    audience: str = "everyone"  # "everyone" or a character slug
    active: bool = True
    exposed_in: int | None = None


class Refusal(BaseModel):
    """A topic the character will not speak to, and why."""

    topic: str
    reason: str = ""


class CastSheet(BaseModel):
    """The full private interior state for one major character."""

    slug: str
    name: str
    canon_ref: str = ""  # canon/characters/<slug>.md
    wants: Wants = Field(default_factory=Wants)
    fears: list[str] = Field(default_factory=list)
    knowledge: list[KnowledgeEntry] = Field(default_factory=list)
    lies: list[Lie] = Field(default_factory=list)
    refusals: list[Refusal] = Field(default_factory=list)
    seed_notes: str = ""  # Wants / Fears section copied from canon at init
    last_updated_chapter: int = 0
    updated_at: float = Field(default_factory=time.time)
