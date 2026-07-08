"""Ship manifest + readiness gate.

`build_manifest` resolves book metadata (title config > premise heuristic >
project_name; author/year/isbn/contact/dedication straight from the `ship:`
config block), fixes the ordered chapter list every format consumes, and
produces a readiness report split into *blockers* (genuine not-dones that
refuse the ship) and *warnings* (surfaced but never refusing).

Blockers: chapter-numbering gaps; chapter statuses outside the shippable set
`{revised, final}`; and -- when the Promise & Motif Ledger (`CanonStore`
`promises()`) is present -- open promise-kind rows, labeled unfired guns. The
`promises()` consume is getattr-guarded: its absence degrades to reporting
every open thread as a warning, never an error.

Warnings: plain kind-less open thread rows. Blocking on those would train
authors to reflexively pass `--allow-incomplete`, destroying the gate's
value -- a texture thread staying open is normal authorial practice, an
unfired gun is a real not-done. `require_ready` is the shared helper every
artifact command calls; it ledgers `ship.check` with the override flag and
refuses on blockers unless `allow_incomplete` is set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..canon.store import CanonStore, slugify
from ..ledger import Ledger
from ..project import WritingProject, split_frontmatter

#: Chapter statuses a finished book may ship with. Anything else is a blocker.
SHIPPABLE_STATUSES = frozenset({"revised", "final"})

#: Promise-kind labels get this human-facing tag in the blocker line.
_UNFIRED_GUN = "unfired gun"


class ShipError(RuntimeError):
    """Raised when a ship command cannot proceed (blockers, missing inputs)."""


@dataclass
class ChapterRef:
    """One chapter's place in the shipped book: number, title, source path."""

    number: int
    title: str
    rel_path: str


@dataclass
class ShipManifest:
    """Resolved book metadata + ordered chapters + readiness report."""

    title: str
    author: str
    year: str
    isbn: str
    contact_lines: list[str]
    dedication: str
    slug: str
    chapters: list[ChapterRef] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True when nothing blocks the ship (warnings do not count)."""
        return not self.blockers


def _premise_title(project: WritingProject) -> str:
    """Best-effort book title from `canon/premise.md`; "" if none is clean.

    Prefers a frontmatter `title`, else the first `# ` H1 with a trailing
    "— Premise"/"- Premise"/"Premise" suffix stripped. The scaffolded
    template's "<name> — Premise" collapses to the bare project name, which
    the caller then treats the same as the project_name fallback.
    """
    try:
        raw = project.read("canon/premise.md")
    except Exception:
        return ""
    fm, body = split_frontmatter(raw)
    fm_title = str(fm.get("title") or "").strip()
    if fm_title:
        return fm_title
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            heading = stripped[2:].strip()
            heading = re.sub(r"\s*[—-]\s*Premise$", "", heading, flags=re.IGNORECASE)
            heading = re.sub(r"\s*Premise$", "", heading, flags=re.IGNORECASE).strip()
            return heading
    return ""


def _resolve_title(project: WritingProject) -> str:
    ship = project.config.ship
    if ship.title.strip():
        return ship.title.strip()
    heuristic = _premise_title(project)
    if heuristic:
        return heuristic
    return project.config.project_name


def _chapter_gaps(numbers: list[int]) -> list[int]:
    """Missing chapter numbers in the range 1..max(present), in order."""
    if not numbers:
        return []
    present = set(numbers)
    return [n for n in range(1, max(numbers) + 1) if n not in present]


def build_manifest(project: WritingProject) -> ShipManifest:
    """Resolve metadata, order chapters, and compute the readiness report."""
    ship = project.config.ship
    title = _resolve_title(project)
    manifest = ShipManifest(
        title=title,
        author=ship.author,
        year=ship.year,
        isbn=ship.isbn,
        contact_lines=list(ship.contact_lines),
        dedication=ship.dedication,
        slug=slugify(title),
    )

    chapters = project.chapters()
    numbers = [c.number for c in chapters]
    for c in chapters:
        rel = f"manuscript/ch-{c.number:02d}.md"
        chapter_title = c.title or f"Chapter {c.number}"
        manifest.chapters.append(ChapterRef(number=c.number, title=chapter_title, rel_path=rel))

    # -- blockers: gaps + non-shippable statuses ------------------------
    for missing in _chapter_gaps(numbers):
        manifest.blockers.append(f"chapter gap: ch-{missing:02d} is missing")
    for c in chapters:
        if c.status not in SHIPPABLE_STATUSES:
            manifest.blockers.append(
                f"ch-{c.number:02d} status {c.status!r} is not shippable "
                f"(need one of {', '.join(sorted(SHIPPABLE_STATUSES))})"
            )

    # -- threads: promise blockers + kind-less warnings -----------------
    store = CanonStore(project)
    open_threads = [t for t in store.threads() if t.status == "open"]
    promises_fn = getattr(store, "promises", None)
    if callable(promises_fn):
        for p in promises_fn():
            if p.status == "open":
                manifest.blockers.append(
                    f"{_UNFIRED_GUN} ({p.kind}) {p.id}: {p.thread}"
                )
        warn_rows = [t for t in open_threads if not t.kind]
    else:
        # Feature 7 absent: no promise semantics available, so every open
        # thread degrades to a warning rather than an error.
        warn_rows = open_threads
    for t in warn_rows:
        opened = f" (opened {t.opened_in})" if t.opened_in else ""
        manifest.warnings.append(f"open thread {t.id}: {t.thread}{opened}")

    return manifest


def require_ready(project: WritingProject, allow_incomplete: bool) -> ShipManifest:
    """Build the manifest, ledger `ship.check`, and refuse on blockers.

    Every artifact command calls this first. `allow_incomplete` overrides a
    blocker refusal and is recorded in the ledger entry so the override is
    auditable (R2, R18). Warnings never refuse.
    """
    manifest = build_manifest(project)
    Ledger(project.root).append(
        "ship.check",
        target=manifest.slug,
        blockers=len(manifest.blockers),
        warnings=len(manifest.warnings),
        allow_incomplete=allow_incomplete,
    )
    if manifest.blockers and not allow_incomplete:
        lines = "\n".join(f"  - {b}" for b in manifest.blockers)
        raise ShipError(
            "manuscript is not ready to ship:\n"
            f"{lines}\n"
            "Fix the blockers, or pass --allow-incomplete to ship anyway."
        )
    return manifest
