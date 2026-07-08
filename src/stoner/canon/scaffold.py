"""scaffold_project: writes the opinionated canon/outline starter files.

Called by `stoner init` immediately after `WritingProject.create()` (which
only creates directories and `stoner.yaml`). Safe to re-run: existing files
are never overwritten, so upgrading an older project just fills in gaps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..project import WritingProject, split_frontmatter

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# project-relative destination -> template filename (relative to templates/)
_RENDERED_FILES: dict[str, str] = {
    "canon/premise.md": "premise.md",
    "canon/style.md": "style.md",
    "canon/timeline.md": "timeline.md",
    "canon/threads.md": "threads.md",
    "canon/motifs.md": "motifs.md",
    "canon/characters/_template.md": "characters/_template.md",
    "canon/world/_template.md": "world/_template.md",
    "canon/facts/_template.md": "facts/_template.md",
    "outline/outline.md": "outline.md",
    "outline/beats/ch-01.md": "beats/ch-01.md",
}


def _read_template(name: str) -> str:
    return (_TEMPLATES_DIR / name).read_text(encoding="utf-8")


def scaffold_project(project: WritingProject, name: str) -> list[str]:
    """Write starter canon/outline files into `project` where missing.

    Returns the project-relative paths actually written. Files that already
    exist are left untouched -- this makes the call idempotent, so it is
    safe to invoke on every `stoner init` and on any later "repair project"
    path without clobbering a writer's edits.
    """
    written: list[str] = []
    for rel, template_name in _RENDERED_FILES.items():
        dest = project.root / rel
        if dest.exists():
            continue
        content = _read_template(template_name)
        if rel == "canon/premise.md":
            content = content.replace("{{PROJECT_NAME}}", name)
        project.write(rel, content)
        written.append(rel)
    return written


def chapter_header_template() -> str:
    """Return the raw manuscript chapter frontmatter+comment header template."""
    return _read_template("manuscript_ch.md")


def new_chapter_stub(number: int, title: str = "", pov: str = "") -> tuple[dict[str, Any], str]:
    """Build (frontmatter, body) for a brand-new chapter from the template.

    Convenience for pipelines that create `manuscript/ch-NN.md` for the
    first time; `WritingProject.write_chapter` takes the resulting pair.
    """
    fm, body = split_frontmatter(chapter_header_template())
    fm["title"] = title
    fm["pov"] = pov
    fm.setdefault("status", "outline")
    fm.setdefault("words", 0)
    return fm, body


def new_beats_stub(number: int) -> str:
    """Beat-sheet template for any chapter (the shipped file only covers ch-01)."""
    content = _read_template("beats/ch-01.md")
    return content.replace("chapter: 1", f"chapter: {number}", 1)


def new_canon_entry(kind: str, name: str) -> tuple[str, str]:
    """Build (project-relative path, content) for a fresh character/world entry.

    Fills the template's empty `name:` field; everything else stays as the
    instructive template for the writer to complete.
    """
    if kind not in ("character", "world"):
        raise ValueError(f"kind must be 'character' or 'world', not {kind!r}")
    template = _read_template(f"{kind}s/_template.md" if kind == "character" else "world/_template.md")
    content = template.replace("name:", f"name: {name}", 1)
    from .store import slugify

    rel = f"canon/{'characters' if kind == 'character' else 'world'}/{slugify(name)}.md"
    return rel, content
