"""CastStore: read/write private cast sheets under `.stoner/cast/`.

This is the *only* module that persists cast state, and it deliberately sits
outside the writer-facing context path. `CanonStore.context_pack`,
`pipelines.common.chapter_context`, `canon.memory`, and
`review.passes.build_context` never call anything here, so private state
cannot reach the writer prompt (the privacy invariant, pinned by a regression
test). Checkers -- the curator, the boundedness check, and the interiority
review pass -- *do* read cast state via `review_digest`, because privacy is a
writer-facing boundary, not a global one; the human sees everything via
`stoner cast show`.

Ids for knowledge and lies are assigned here, sequentially per sheet
(`k001`, `l001`, ...), never by a model.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from ..canon.store import CanonStore, extract_section
from ..project import WritingProject
from .sheet import CastSheet, KnowledgeEntry

_CAST_DIR = ".stoner/cast"
_DEFAULT_DIGEST_CHARS = 4000
_SEED_SECTION = "Wants / Fears"


class CastError(RuntimeError):
    """Raised for a missing/corrupt sheet or an invalid store operation."""


class CastStore:
    """Load/save/list cast sheets for one project, and seed them from canon."""

    def __init__(self, project: WritingProject):
        self.project = project

    # -- paths ----------------------------------------------------------

    def _rel(self, slug: str) -> str:
        return f"{_CAST_DIR}/{slug}.json"

    @property
    def _dir(self) -> Path:
        return self.project.root / _CAST_DIR

    def exists(self, slug: str) -> bool:
        return self.project.resolve(self._rel(slug)).exists()

    # -- load / save ----------------------------------------------------

    def load(self, slug: str) -> CastSheet:
        path = self.project.resolve(self._rel(slug))
        if not path.exists():
            raise CastError(f"no cast sheet for {slug!r} at {self._rel(slug)}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise CastError(f"cast sheet {self._rel(slug)} is not valid JSON: {e}") from None
        try:
            return CastSheet.model_validate(raw)
        except ValidationError as e:
            raise CastError(f"cast sheet {self._rel(slug)} failed validation: {e}") from None

    def save(self, sheet: CastSheet) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self.project.write(self._rel(sheet.slug), sheet.model_dump_json(indent=2))

    # -- listing --------------------------------------------------------

    def list_slugs(self) -> list[str]:
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.json"))

    def list_sheets(self) -> list[CastSheet]:
        return [self.load(slug) for slug in self.list_slugs()]

    # -- creation from canon -------------------------------------------

    def init_from_canon(self, name: str) -> CastSheet:
        """Create a sheet seeded from an existing canon character. No model call.

        Copies the canon `## Wants / Fears` section into `seed_notes` for the
        human to structure into wants/fears/lies, and refuses to clobber an
        existing sheet.
        """
        entry = CanonStore(self.project).find_character_by_name(name)
        if entry is None:
            raise CastError(
                f"no canon character named {name!r}. Create it first with "
                f"`stoner canon new character \"{name}\"`."
            )
        slug = entry.slug
        if self.exists(slug):
            raise CastError(
                f"cast sheet {self._rel(slug)} already exists -- refusing to "
                "overwrite. Edit it directly or remove it first."
            )
        sheet = CastSheet(
            slug=slug,
            name=entry.name,
            canon_ref=entry.rel_path,
            seed_notes=extract_section(entry.body, _SEED_SECTION),
        )
        self.save(sheet)
        return sheet

    # -- id assignment --------------------------------------------------

    @staticmethod
    def next_knowledge_id(sheet: CastSheet) -> str:
        return f"k{_next_num(e.id for e in sheet.knowledge):03d}"

    @staticmethod
    def next_lie_id(sheet: CastSheet) -> str:
        return f"l{_next_num(lie.id for lie in sheet.lies):03d}"


# ---------------------------------------------------------------------------
# Pure helpers over a sheet (no I/O)
# ---------------------------------------------------------------------------


def _next_num(ids) -> int:
    """Highest trailing integer across `ids`, plus one (1 when empty)."""
    highest = 0
    for id_ in ids:
        digits = "".join(ch for ch in id_ if ch.isdigit())
        if digits:
            highest = max(highest, int(digits))
    return highest + 1


def knowledge_asof(sheet: CastSheet, chapter: int) -> list[KnowledgeEntry]:
    """The knowledge a character holds as of `chapter`: `learned_in <= chapter`.

    Backstory (`learned_in == 0`) is always included.
    """
    return [e for e in sheet.knowledge if e.learned_in <= chapter]


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def _knowledge_line(e: KnowledgeEntry) -> str:
    when = "backstory" if e.learned_in == 0 else f"ch-{e.learned_in:02d}"
    secret = " [secret]" if e.secret else ""
    return f"- ({e.id}) {e.fact} — learned {when} ({e.how}){secret}"


def _lie_line(lie) -> str:  # type: ignore[no-untyped-def]
    state = "active" if lie.active else "dropped"
    exposed = f", exposed ch-{lie.exposed_in:02d}" if lie.exposed_in is not None else ""
    return (
        f"- ({lie.id}) claims \"{lie.claim}\" to {lie.audience} "
        f"(truth: {lie.truth or '—'}; {state}{exposed})"
    )


def _sheet_header(sheet: CastSheet) -> list[str]:
    lines = [f"### {sheet.name} ({sheet.slug})"]
    if sheet.wants.is_set():
        lines.append(f"Wants — stated: {sheet.wants.stated or '—'} / real: {sheet.wants.real or '—'}")
    if sheet.fears:
        lines.append("Fears: " + "; ".join(sheet.fears))
    return lines


def private_digest(sheet: CastSheet, chapter: int, max_chars: int = _DEFAULT_DIGEST_CHARS) -> str:
    """Render ONE character's bounded private view for a scene prompt.

    Only knowledge learned on or before `chapter` is shown -- a character
    cannot role-play with knowledge they don't yet have. When the render
    exceeds `max_chars`, the oldest non-secret knowledge entries are dropped
    first (secrets and the most recent state are the load-bearing parts),
    mirroring `Memory.rebuild_book_so_far`. Never contains another
    character's data.
    """
    known = knowledge_asof(sheet, chapter)

    def render(entries: list[KnowledgeEntry]) -> str:
        lines = _sheet_header(sheet)
        if entries:
            lines.append("Knows:")
            lines.extend(_knowledge_line(e) for e in entries)
        active_lies = [lie for lie in sheet.lies if lie.active]
        if active_lies:
            lines.append("Lies (maintain these):")
            lines.extend(_lie_line(lie) for lie in active_lies)
        if sheet.refusals:
            lines.append("Refuses to discuss:")
            lines.extend(f"- {r.topic} ({r.reason})" if r.reason else f"- {r.topic}" for r in sheet.refusals)
        return "\n".join(lines)

    text = render(known)
    # Drop oldest non-secret knowledge first until it fits (secrets survive).
    droppable = sorted(
        (i for i, e in enumerate(known) if not e.secret),
        key=lambda i: (known[i].learned_in, known[i].id),
    )
    while len(text) > max_chars and droppable:
        known.pop(droppable.pop(0))
        droppable = sorted(
            (i for i, e in enumerate(known) if not e.secret),
            key=lambda i: (known[i].learned_in, known[i].id),
        )
        text = render(known)
    return text[:max_chars]


def review_digest(project: WritingProject, chapter: int, max_chars: int = _DEFAULT_DIGEST_CHARS) -> str:
    """Render ALL cast sheets with full ledgers for checker roles.

    Unlike `private_digest`, this shows every knowledge entry (with its
    `learned_in` label) across every character -- the curator, boundedness
    check, and interiority pass all need the complete ledger. Checkers seeing
    private state is correct: privacy is a writer-facing boundary only. Empty
    when no sheets exist, so callers can branch on `""`.
    """
    store = CastStore(project)
    sheets = store.list_sheets()
    if not sheets:
        return ""
    blocks: list[str] = [f"(Cast sheets as of chapter {chapter}. PRIVATE — checker use only.)"]
    for sheet in sheets:
        lines = _sheet_header(sheet)
        if sheet.knowledge:
            lines.append("Knowledge ledger:")
            lines.extend(_knowledge_line(e) for e in sheet.knowledge)
        if sheet.lies:
            lines.append("Lies:")
            lines.extend(_lie_line(lie) for lie in sheet.lies)
        if sheet.refusals:
            lines.append("Refusals:")
            lines.extend(f"- {r.topic} ({r.reason})" if r.reason else f"- {r.topic}" for r in sheet.refusals)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)[:max_chars]
