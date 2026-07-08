"""Dependency-free, byte-reproducible EPUB3 writer (R6, R9).

ebooklib is AGPL, disqualifying for an MIT tool, so this is hand-rolled on
stdlib `zipfile` + `xml.sax.saxutils`. An EPUB3 is a zip: an uncompressed
`mimetype` first entry, `META-INF/container.xml`, an OPF package, a nav
document, and one XHTML per page/chapter, plus an embedded stylesheet.

Reproducibility (two runs on the same inputs are byte-identical): every zip
entry's timestamp is pinned to 1980-01-01, entries are written in a fixed
order, `dc:identifier` is a UUID5 of project-name+title, and
`dcterms:modified` is a fixed epoch. This is the one format that never
degrades -- it has no optional dependency to be missing.
"""

from __future__ import annotations

import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from ..ledger import Ledger
from ..project import WritingProject
from .assemble import Book, BookPage, Chapter, Heading, Paragraph, SceneBreak, assemble_book
from .manifest import ShipManifest, require_ready

#: Fixed timestamp for every zip entry (zip epoch floor) -> reproducible bytes.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
#: Pinned modification time so the OPF (and thus the archive) is stable.
_MODIFIED = "1980-01-01T00:00:00Z"
#: Stable namespace for deriving a per-book UUID5 identifier.
_UUID_NAMESPACE = uuid.UUID("6b1d5f8e-2c4a-4a2e-9f3b-1e0d7c6a5b40")

_STYLESHEET = """\
body { font-family: Georgia, 'Times New Roman', serif; line-height: 1.5; margin: 5%; }
h1 { text-align: center; margin: 2em 0 1.5em; font-weight: normal; }
p { margin: 0; text-indent: 1.5em; }
p.first { text-indent: 0; }
hr.scene-break { border: none; text-align: center; margin: 1.5em 0; }
hr.scene-break::after { content: "* * *"; letter-spacing: 0.5em; }
.frontmatter { text-align: center; margin-top: 25%; }
.frontmatter p { text-indent: 0; margin: 0.4em 0; }
"""


def book_identifier(project_name: str, title: str) -> str:
    """Deterministic per-book urn:uuid identifier (UUID5)."""
    seed = f"{project_name}\x00{title}"
    return f"urn:uuid:{uuid.uuid5(_UUID_NAMESPACE, seed)}"


# ---------------------------------------------------------------------------
# XHTML rendering
# ---------------------------------------------------------------------------


def _inline_html(paragraph: Paragraph) -> str:
    out: list[str] = []
    for run in paragraph.runs:
        text = escape(run.text)
        if run.style == "em":
            out.append(f"<em>{text}</em>")
        elif run.style == "strong":
            out.append(f"<strong>{text}</strong>")
        else:
            out.append(text)
    return "".join(out)


def _xhtml_document(title: str, body_inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">\n'
        f"<head><meta charset=\"utf-8\"/><title>{escape(title)}</title>"
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f"<body>\n{body_inner}\n</body>\n</html>\n"
    )


def _chapter_xhtml(chapter: Chapter) -> str:
    parts = [f"<section class=\"chapter\">\n<h1>{escape(chapter.title)}</h1>"]
    first_para = True
    for block in chapter.blocks:
        if isinstance(block, Paragraph):
            cls = ' class="first"' if first_para else ""
            parts.append(f"<p{cls}>{_inline_html(block)}</p>")
            first_para = False
        elif isinstance(block, SceneBreak):
            parts.append('<hr class="scene-break"/>')
            first_para = True
        elif isinstance(block, Heading):
            level = min(max(block.level, 2), 6)
            parts.append(f"<h{level}>{escape(block.text)}</h{level}>")
    parts.append("</section>")
    return _xhtml_document(chapter.title, "\n".join(parts))


def _page_xhtml(page: BookPage) -> str:
    lines = "\n".join(f"<p>{escape(line)}</p>" if line else "<p>&#160;</p>" for line in page.lines)
    body = f'<section class="frontmatter {page.kind}">\n{lines}\n</section>'
    return _xhtml_document(page.kind, body)


# ---------------------------------------------------------------------------
# OPF + nav
# ---------------------------------------------------------------------------


def _container_xml() -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles>\n'
        '    <rootfile full-path="OEBPS/package.opf" '
        'media-type="application/oebps-package+xml"/>\n'
        '  </rootfiles>\n'
        '</container>\n'
    )


def _package_opf(book: Book, identifier: str, files: list[tuple[str, str]]) -> str:
    manifest_items = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine_items: list[str] = []
    for idx, (name, _content) in enumerate(files):
        item_id = f"item{idx:03d}"
        manifest_items.append(
            f'    <item id="{item_id}" href="{name}" media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'    <itemref idref="{item_id}"/>')
    creator = (
        f'    <dc:creator id="creator">{escape(book.author)}</dc:creator>\n'
        if book.author
        else ""
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">{escape(identifier)}</dc:identifier>\n'
        f'    <dc:title>{escape(book.title)}</dc:title>\n'
        '    <dc:language>en</dc:language>\n'
        f"{creator}"
        f'    <meta property="dcterms:modified">{_MODIFIED}</meta>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        + "\n".join(manifest_items)
        + '\n  </manifest>\n'
        '  <spine>\n'
        + "\n".join(spine_items)
        + '\n  </spine>\n'
        '</package>\n'
    )


def _nav_xhtml(book: Book, chapter_files: list[tuple[str, str]]) -> str:
    items = "\n".join(
        f'      <li><a href="{name}">{escape(title)}</a></li>'
        for name, title in chapter_files
    )
    body = (
        '<nav epub:type="toc" id="toc">\n'
        '    <h1>Contents</h1>\n'
        '    <ol>\n'
        f"{items}\n"
        '    </ol>\n'
        '  </nav>'
    )
    return _xhtml_document("Contents", body)


# ---------------------------------------------------------------------------
# Assembly into a zip
# ---------------------------------------------------------------------------


def build_epub_bytes(book: Book, identifier: str) -> bytes:
    """Render `book` to EPUB3 bytes (byte-identical for identical inputs)."""
    # Content documents in reading order, with stable filenames.
    content: list[tuple[str, str]] = []
    for i, page in enumerate(book.front_matter):
        content.append((f"front-{i:02d}.xhtml", _page_xhtml(page)))
    chapter_nav: list[tuple[str, str]] = []
    for chapter in book.chapters:
        name = f"chap-{chapter.number:03d}.xhtml"
        content.append((name, _chapter_xhtml(chapter)))
        chapter_nav.append((name, chapter.title))
    for i, page in enumerate(book.back_matter):
        content.append((f"back-{i:02d}.xhtml", _page_xhtml(page)))

    opf = _package_opf(book, identifier, content)
    nav = _nav_xhtml(book, chapter_nav)

    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        _write_entry(zf, "mimetype", "application/epub+zip", compress=False)
        _write_entry(zf, "META-INF/container.xml", _container_xml())
        _write_entry(zf, "OEBPS/style.css", _STYLESHEET)
        _write_entry(zf, "OEBPS/package.opf", opf)
        _write_entry(zf, "OEBPS/nav.xhtml", nav)
        for name, doc in content:
            _write_entry(zf, f"OEBPS/{name}", doc)
    return buffer.getvalue()


def _write_entry(zf: zipfile.ZipFile, name: str, data: str, compress: bool = True) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data.encode("utf-8"))


def write_epub(
    project: WritingProject,
    allow_incomplete: bool = False,
    dest: Path | None = None,
    manifest: ShipManifest | None = None,
) -> tuple[Path, int]:
    """Gate, assemble, and write the EPUB; ledger `ship.epub`. Returns
    (path, chapter_count). A pre-built `manifest` skips the gate (used by
    `ship all`, which gates once for the whole run)."""
    if manifest is None:
        manifest = require_ready(project, allow_incomplete)
    book = assemble_book(project, manifest)
    identifier = book_identifier(project.config.project_name, manifest.title)
    data = build_epub_bytes(book, identifier)
    out = dest or project.resolve(f"export/{manifest.slug}.epub")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    Ledger(project.root).append(
        "ship.epub", target=str(out.relative_to(project.root)), chapters=len(book.chapters)
    )
    return out, len(book.chapters)
