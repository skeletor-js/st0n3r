"""Manuscript assembly + a feature-local prose-markdown subset parser.

One canonical `Book` structure -- front matter, parsed chapters, back matter
-- that every document format (EPUB/PDF/DOCX) consumes, so formats never
disagree about content (R4). The parser (R5) handles exactly the subset the
harness itself writes and the corpus uses: blank-line-separated paragraphs,
inline `*em*`/`**strong**` runs, `* * *` / `***` / `---` scene breaks, and
`#`-prefixed headings. Everything else passes through as plain text -- an
unbalanced `*` degrades to a literal asterisk rather than eating the line.

Pure functions, no I/O beyond `project.read_chapter`, no provider, no extra
dependency -- testable with nothing installed. In the slop-analyzer
hand-rolled tradition (module-level constants, one screen of scanning code).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..project import WritingProject
from .manifest import ShipManifest

#: A line equal (after strip) to one of these is a scene break, not prose.
SCENE_BREAKS = frozenset({"* * *", "***", "---", "* * * *"})

_HEADING_RE = re.compile(r"^(#+)\s+(.*)$")
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")


# ---------------------------------------------------------------------------
# Block model
# ---------------------------------------------------------------------------


@dataclass
class InlineRun:
    """A styled span of a paragraph. `style` is plain | em | strong."""

    text: str
    style: str = "plain"


@dataclass
class Paragraph:
    runs: list[InlineRun] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The paragraph's plain text (styling markers dropped)."""
        return "".join(r.text for r in self.runs)


@dataclass
class SceneBreak:
    pass


@dataclass
class Heading:
    level: int
    text: str


Block = Paragraph | SceneBreak | Heading


@dataclass
class Chapter:
    number: int
    title: str
    blocks: list[Block] = field(default_factory=list)

    def plain_text(self) -> str:
        """Concatenated paragraph text, scene breaks normalized to `* * *`."""
        parts: list[str] = []
        for block in self.blocks:
            if isinstance(block, Paragraph):
                parts.append(block.text)
            elif isinstance(block, Heading):
                parts.append(block.text)
            else:
                parts.append("* * *")
        return "\n\n".join(parts)


@dataclass
class BookPage:
    """One front/back-matter page: a kind plus already-resolved display lines."""

    kind: str  # half-title | title | copyright | dedication | end
    lines: list[str] = field(default_factory=list)


@dataclass
class Book:
    title: str
    author: str
    front_matter: list[BookPage] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    back_matter: list[BookPage] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Inline parsing
# ---------------------------------------------------------------------------


def parse_inline(text: str) -> list[InlineRun]:
    """Split a paragraph line into plain/em/strong runs.

    `**x**` is strong, `*x*` is em. An asterisk with no matching close (or an
    empty span) is emitted literally -- unbalanced emphasis never swallows
    text or raises.
    """
    runs: list[InlineRun] = []
    buf: list[str] = []
    i, n = 0, len(text)

    def flush() -> None:
        if buf:
            runs.append(InlineRun("".join(buf), "plain"))
            buf.clear()

    while i < n:
        if text.startswith("**", i):
            close = text.find("**", i + 2)
            if close > i + 2:
                flush()
                runs.append(InlineRun(text[i + 2 : close], "strong"))
                i = close + 2
                continue
            buf.append("*")
            i += 1
            continue
        if text[i] == "*":
            close = text.find("*", i + 1)
            if close > i + 1:
                flush()
                runs.append(InlineRun(text[i + 1 : close], "em"))
                i = close + 1
                continue
            buf.append("*")
            i += 1
            continue
        buf.append(text[i])
        i += 1
    flush()
    return runs


# ---------------------------------------------------------------------------
# Block parsing
# ---------------------------------------------------------------------------


def parse_prose(body: str) -> list[Block]:
    """Parse a chapter body (frontmatter already stripped) into blocks."""
    blocks: list[Block] = []
    for chunk in _PARA_SPLIT_RE.split(body.strip("\n")):
        stripped = chunk.strip()
        if not stripped:
            continue
        if stripped in SCENE_BREAKS:
            blocks.append(SceneBreak())
            continue
        lines = chunk.splitlines()
        if len(lines) == 1:
            m = _HEADING_RE.match(lines[0].strip())
            if m:
                blocks.append(Heading(level=len(m.group(1)), text=m.group(2).strip()))
                continue
        text = " ".join(ln.strip() for ln in lines if ln.strip())
        blocks.append(Paragraph(runs=parse_inline(text)))
    return blocks


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _front_matter(manifest: ShipManifest) -> list[BookPage]:
    pages: list[BookPage] = [BookPage("half-title", [manifest.title])]

    title_lines = [manifest.title]
    if manifest.author:
        title_lines += ["", manifest.author]
    pages.append(BookPage("title", title_lines))

    copyright_lines: list[str] = []
    holder = manifest.author or manifest.title
    year = f" {manifest.year}" if manifest.year else ""
    copyright_lines.append(f"Copyright ©{year} {holder}".strip())
    copyright_lines.append("All rights reserved.")
    copyright_lines.append(f"ISBN: {manifest.isbn}" if manifest.isbn else "ISBN: [to be assigned]")
    pages.append(BookPage("copyright", copyright_lines))

    if manifest.dedication.strip():
        pages.append(BookPage("dedication", [manifest.dedication.strip()]))
    return pages


def assemble_book(project: WritingProject, manifest: ShipManifest) -> Book:
    """Build the canonical `Book` from the manifest's ordered chapters."""
    book = Book(title=manifest.title, author=manifest.author)
    book.front_matter = _front_matter(manifest)
    for ref in manifest.chapters:
        _fm, body = project.read_chapter(ref.number)
        book.chapters.append(
            Chapter(number=ref.number, title=ref.title, blocks=parse_prose(body))
        )
    book.back_matter = [BookPage("end", ["THE END"])]
    return book
