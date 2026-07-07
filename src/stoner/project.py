"""WritingProject: the opinionated on-disk layout of a st0n3r project.

All harness file access goes through this class so paths stay jailed to the
project root and layout conventions live in one place.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import CONFIG_FILENAME, StonerConfig

CHAPTER_RE = re.compile(r"^ch-(\d{2,3})\.md$")

DIRS = [
    "canon",
    "canon/characters",
    "canon/world",
    "outline",
    "outline/beats",
    "manuscript",
    "notes",
    ".stoner",
    ".stoner/sessions",
    ".stoner/reviews",
]


class ProjectError(RuntimeError):
    pass


@dataclass
class ChapterInfo:
    path: Path
    number: int
    title: str = ""
    status: str = "draft"
    pov: str = ""
    words: int = 0
    frontmatter: dict[str, Any] = field(default_factory=dict)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split leading YAML frontmatter from a markdown document."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            fm_raw = text[4:end]
            body = text[end + 4 :].lstrip("\n")
            try:
                fm = yaml.safe_load(fm_raw) or {}
            except yaml.YAMLError:
                fm = {}
            if isinstance(fm, dict):
                return fm, body
    return {}, text


def join_frontmatter(fm: dict[str, Any], body: str) -> str:
    if not fm:
        return body
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body.lstrip("\n")


def count_words(text: str) -> int:
    return len(re.findall(r"\b[\w''-]+\b", text))


class WritingProject:
    """Handle to a st0n3r writing project on disk."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not (self.root / CONFIG_FILENAME).exists():
            raise ProjectError(
                f"No {CONFIG_FILENAME} found in {self.root}. Run `stoner init` first."
            )
        self.config = StonerConfig.load(self.root)

    # -- discovery ------------------------------------------------------
    @classmethod
    def find(cls, start: Path | None = None) -> "WritingProject":
        """Walk upward from `start` (or cwd) until stoner.yaml is found."""
        cur = (start or Path.cwd()).resolve()
        for candidate in [cur, *cur.parents]:
            if (candidate / CONFIG_FILENAME).exists():
                return cls(candidate)
        raise ProjectError(
            f"No st0n3r project found at or above {cur}. Run `stoner init <name>`."
        )

    @classmethod
    def create(cls, root: Path, name: str) -> "WritingProject":
        """Scaffold a fresh project. Templates are filled in by canon.templates."""
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        if (root / CONFIG_FILENAME).exists():
            raise ProjectError(f"{root} already contains a st0n3r project")
        for d in DIRS:
            (root / d).mkdir(parents=True, exist_ok=True)
        cfg = StonerConfig(project_name=name)
        (root / CONFIG_FILENAME).write_text(cfg.dump_yaml(), encoding="utf-8")
        return cls(root)

    # -- path jail ------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        """Resolve a project-relative path, refusing escapes."""
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise ProjectError(f"Path escapes project root: {rel}")
        return p

    def read(self, rel: str) -> str:
        p = self.resolve(rel)
        if not p.exists():
            raise ProjectError(f"Not found: {rel}")
        return p.read_text(encoding="utf-8")

    def write(self, rel: str, content: str) -> Path:
        p = self.resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    # -- chapters -------------------------------------------------------
    def chapter_rel(self, number: int) -> str:
        return f"manuscript/ch-{number:02d}.md"

    def chapters(self) -> list[ChapterInfo]:
        out: list[ChapterInfo] = []
        mdir = self.root / "manuscript"
        if not mdir.exists():
            return out
        for f in sorted(mdir.iterdir()):
            m = CHAPTER_RE.match(f.name)
            if not m:
                continue
            fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
            out.append(
                ChapterInfo(
                    path=f,
                    number=int(m.group(1)),
                    title=str(fm.get("title", "")),
                    status=str(fm.get("status", "draft")),
                    pov=str(fm.get("pov", "")),
                    words=count_words(body),
                    frontmatter=fm,
                )
            )
        return out

    def read_chapter(self, number: int) -> tuple[dict[str, Any], str]:
        return split_frontmatter(self.read(self.chapter_rel(number)))

    def write_chapter(self, number: int, fm: dict[str, Any], body: str) -> Path:
        fm = dict(fm)
        fm.setdefault("title", "")
        fm.setdefault("status", "draft")
        fm["words"] = count_words(body)
        return self.write(self.chapter_rel(number), join_frontmatter(fm, body))

    # -- memory ---------------------------------------------------------
    @property
    def memory_path(self) -> Path:
        return self.root / ".stoner" / "memory.json"

    def read_memory(self) -> dict[str, Any]:
        if self.memory_path.exists():
            try:
                return json.loads(self.memory_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def write_memory(self, mem: dict[str, Any]) -> None:
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory_path.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- misc -----------------------------------------------------------
    def status_summary(self) -> dict[str, Any]:
        chapters = self.chapters()
        return {
            "name": self.config.project_name,
            "root": str(self.root),
            "chapters": len(chapters),
            "words": sum(c.words for c in chapters),
            "by_status": {
                s: sum(1 for c in chapters if c.status == s)
                for s in sorted({c.status for c in chapters})
            },
        }
