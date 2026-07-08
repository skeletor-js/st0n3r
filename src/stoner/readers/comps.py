"""Public-domain comp management: ingest a local text, split into chapters.

Comps are project-local and user-supplied. `add_comp` takes a local text or
markdown file plus title/author/year/source metadata, splits it into chapters
(a chapter-heading regex first, a fixed word-count fallback), and writes
`comps/<slug>/ch-NN.md` plus a `comp.json` metadata file. Nothing ships in the
wheel and there is no network fetch in v1 (invariant 9, invariant 12); acquiring
public-domain texts and the attribution convention are docs-only, in
`docs/CREDITS.md`. Re-adding a comp refuses to overwrite without `force`
(invariant 11, the scaffold idempotence posture).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from pydantic import BaseModel, Field

from ..ledger import Ledger
from ..project import WritingProject

#: Word-count fallback chunk size when no chapter headings are found.
_FALLBACK_WORDS = 1500

_CHAPTER_HEADING = re.compile(r"(?i)^\s*chapter\b")
_ROMAN_HEADING = re.compile(r"^[IVXLCDM]{1,7}\.?$")


class CompMeta(BaseModel):
    """Metadata for one ingested comp (`comps/<slug>/comp.json`)."""

    slug: str
    title: str
    author: str = ""
    year: str = ""
    source: str = ""
    chapters: int = 0
    created_at: float = Field(default_factory=time.time)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "comp"


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if _CHAPTER_HEADING.match(s):
        return True
    return bool(_ROMAN_HEADING.match(s))


def split_chapters(text: str) -> list[str]:
    """Split a comp text into chapter bodies. Uses chapter-heading lines when
    there are at least two; otherwise falls back to fixed word-count chunks."""
    lines = text.splitlines()
    heads = [i for i, ln in enumerate(lines) if _is_heading(ln)]
    if len(heads) >= 2:
        chunks: list[str] = []
        for j, start in enumerate(heads):
            end = heads[j + 1] if j + 1 < len(heads) else len(lines)
            body = "\n".join(lines[start + 1 : end]).strip()
            if body:
                chunks.append(body)
        if chunks:
            return chunks
    return _wordcount_split(text)


def _wordcount_split(text: str, size: int = _FALLBACK_WORDS) -> list[str]:
    words = text.split()
    if not words:
        return []
    return [" ".join(words[i : i + size]) for i in range(0, len(words), size)]


def comps_dir(project: WritingProject) -> Path:
    return project.root / "comps"


def comp_dir(project: WritingProject, slug: str) -> Path:
    return comps_dir(project) / slug


def add_comp(
    project: WritingProject,
    source_path: Path,
    *,
    title: str,
    author: str = "",
    year: str = "",
    source: str = "",
    slug: str | None = None,
    force: bool = False,
) -> CompMeta:
    """Ingest a local PD text file as a comp. Refuses to overwrite an existing
    comp without `force`. Ledgers `readers.comps.add`."""
    if not source_path.exists():
        raise ValueError(f"comp source not found: {source_path}")
    slug = slug or _slugify(title)
    cdir = comp_dir(project, slug)
    meta_path = cdir / "comp.json"
    if meta_path.exists() and not force:
        raise ValueError(f"comp {slug!r} already exists; pass force=True to overwrite")

    text = source_path.read_text(encoding="utf-8")
    chapters = split_chapters(text)
    if not chapters:
        raise ValueError(f"comp source {source_path} produced no chapters (empty text?)")

    # Clear any stale chapter files from a previous ingest before rewriting.
    if cdir.exists():
        for old in cdir.glob("ch-*.md"):
            old.unlink()
    cdir.mkdir(parents=True, exist_ok=True)
    for i, body in enumerate(chapters, start=1):
        (cdir / f"ch-{i:02d}.md").write_text(body.strip() + "\n", encoding="utf-8")

    meta = CompMeta(
        slug=slug, title=title, author=author, year=year, source=source, chapters=len(chapters)
    )
    meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    Ledger(project.root).append("readers.comps.add", target=slug, chapters=len(chapters))
    return meta


def list_comps(project: WritingProject) -> list[CompMeta]:
    """All ingested comps, by slug."""
    base = comps_dir(project)
    if not base.exists():
        return []
    out: list[CompMeta] = []
    for d in sorted(base.iterdir()):
        meta_path = d / "comp.json"
        if not meta_path.exists():
            continue
        try:
            out.append(CompMeta.model_validate_json(meta_path.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


def load_comp(project: WritingProject, slug: str) -> CompMeta:
    meta_path = comp_dir(project, slug) / "comp.json"
    if not meta_path.exists():
        raise ValueError(f"no comp named {slug!r} (add one with `stoner readers comps add`)")
    return CompMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))


def load_comp_chapters(project: WritingProject, slug: str) -> list[str]:
    """Comp chapter bodies in order (ch-01.md, ch-02.md, ...)."""
    cdir = comp_dir(project, slug)
    if not cdir.exists():
        raise ValueError(f"no comp named {slug!r}")
    out: list[str] = []
    for f in sorted(cdir.glob("ch-*.md")):
        out.append(f.read_text(encoding="utf-8"))
    return out
