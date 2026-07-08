"""Trade-paperback interior PDF via reportlab, behind the `export` extra (R7).

reportlab is imported lazily inside the build function; a missing extra fails
*only* this format with an actionable message naming
`pip install 'st0n3r[export]'` (R9) -- EPUB and every other command are
unaffected. The interior uses Platypus page templates: a sunk-title chapter
opener, and mirrored verso/recto body pages with running heads (author on the
verso, title on the recto) and folios. Trim parses from `ship.trim`
(default US-digest 5.5x8.5in). reportlab's invariant mode pins the creation
date and document id so repeated builds are stable.
"""

from __future__ import annotations

import io
from pathlib import Path
from xml.sax.saxutils import escape

from ..ledger import Ledger
from ..project import WritingProject
from .assemble import Book, Heading, Paragraph, SceneBreak, assemble_book
from .manifest import ShipError, ShipManifest, require_ready

_INSTALL_HINT = (
    "PDF export needs the optional reportlab dependency. Install it with:\n"
    "    pip install 'st0n3r[export]'\n"
    "Other formats (epub, docx) are unaffected."
)

# Margins in points (72pt = 1in). Inner (gutter/binding) > outer.
_TOP = 54.0
_BOTTOM = 54.0
_INNER = 54.0
_OUTER = 36.0


def _parse_trim(trim: str) -> tuple[float, float]:
    """`"5.5x8.5"` inches -> (width_pt, height_pt). Falls back to US digest."""
    try:
        w_str, h_str = trim.lower().replace(" ", "").split("x", 1)
        return float(w_str) * 72.0, float(h_str) * 72.0
    except (ValueError, AttributeError):
        return 5.5 * 72.0, 8.5 * 72.0


def _inline_markup(paragraph: Paragraph) -> str:
    """Render a paragraph's runs as reportlab mini-markup (escaped)."""
    out: list[str] = []
    for run in paragraph.runs:
        text = escape(run.text)
        if run.style == "em":
            out.append(f"<i>{text}</i>")
        elif run.style == "strong":
            out.append(f"<b>{text}</b>")
        else:
            out.append(text)
    return "".join(out)


def build_pdf_bytes(book: Book, trim: str = "5.5x8.5") -> bytes:
    """Render `book` to trade-paperback PDF bytes. Raises ShipError if the
    export extra is missing."""
    try:
        import reportlab.rl_config as rl_config
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import (
            BaseDocTemplate,
            Frame,
            NextPageTemplate,
            PageBreak,
            PageTemplate,
            Spacer,
        )
        from reportlab.platypus import Paragraph as RLParagraph
    except ImportError as e:
        raise ShipError(_INSTALL_HINT) from e

    # Reproducible output: pins CreationDate + document id (R7).
    rl_config.invariant = 1

    page_w, page_h = _parse_trim(trim)
    frame_w = page_w - _INNER - _OUTER
    frame_h = page_h - _TOP - _BOTTOM
    author = book.author or ""
    title = book.title or ""

    def _folio(canvas: object, page_num: int) -> None:
        canvas.setFont("Times-Roman", 9)  # type: ignore[attr-defined]
        canvas.drawCentredString(page_w / 2.0, _BOTTOM / 2.0, str(page_num))  # type: ignore[attr-defined]

    def _running_head(canvas: object, text: str) -> None:
        if not text:
            return
        canvas.setFont("Times-Roman", 9)  # type: ignore[attr-defined]
        canvas.drawCentredString(page_w / 2.0, page_h - _TOP / 2.0, text)  # type: ignore[attr-defined]

    def on_frontmatter(canvas: object, doc: object) -> None:
        pass  # no folio, no running head on front matter

    def on_opener(canvas: object, doc: object) -> None:
        _folio(canvas, canvas.getPageNumber())  # type: ignore[attr-defined]

    def on_verso(canvas: object, doc: object) -> None:
        _running_head(canvas, author)
        _folio(canvas, canvas.getPageNumber())  # type: ignore[attr-defined]

    def on_recto(canvas: object, doc: object) -> None:
        _running_head(canvas, title)
        _folio(canvas, canvas.getPageNumber())  # type: ignore[attr-defined]

    # Verso (even/left): inner margin on the right -> frame flush to outer.
    verso_frame = Frame(_OUTER, _BOTTOM, frame_w, frame_h, id="verso")
    # Recto (odd/right): inner margin on the left (binding side).
    recto_frame = Frame(_INNER, _BOTTOM, frame_w, frame_h, id="recto")
    plain_frame = Frame(_INNER, _BOTTOM, frame_w, frame_h, id="plain")
    # Opener frame is sunk so the chapter title starts a third of the way down.
    sunk = page_h * 0.24
    opener_frame = Frame(_INNER, _BOTTOM, frame_w, frame_h - sunk, id="opener")

    templates = [
        PageTemplate(id="frontmatter", frames=[plain_frame], onPage=on_frontmatter),
        PageTemplate(id="opener", frames=[opener_frame], onPage=on_opener),
        PageTemplate(id="verso", frames=[verso_frame], onPage=on_verso),
        PageTemplate(id="recto", frames=[recto_frame], onPage=on_recto),
    ]

    body_style = ParagraphStyle(
        "Body", fontName="Times-Roman", fontSize=11, leading=15,
        alignment=TA_JUSTIFY, firstLineIndent=18, spaceAfter=0,
    )
    first_body_style = ParagraphStyle("BodyFirst", parent=body_style, firstLineIndent=0)
    opener_title_style = ParagraphStyle(
        "OpenerTitle", fontName="Times-Roman", fontSize=20, leading=24,
        alignment=TA_CENTER, spaceAfter=28,
    )
    fm_style = ParagraphStyle(
        "FrontMatter", fontName="Times-Roman", fontSize=13, leading=20, alignment=TA_CENTER,
    )
    break_style = ParagraphStyle(
        "SceneBreak", fontName="Times-Roman", fontSize=12, leading=18,
        alignment=TA_CENTER, spaceBefore=12, spaceAfter=12,
    )
    heading_style = ParagraphStyle(
        "Heading", fontName="Times-Roman", fontSize=14, leading=18,
        alignment=TA_CENTER, spaceBefore=16, spaceAfter=8,
    )

    story: list[object] = [NextPageTemplate("frontmatter")]
    for i, page in enumerate(book.front_matter):
        if i > 0:
            story.append(PageBreak())
        story.append(Spacer(1, page_h * 0.22))
        for line in page.lines:
            story.append(RLParagraph(escape(line) or " ", fm_style))

    for chapter in book.chapters:
        story.append(NextPageTemplate("opener"))
        story.append(PageBreak())
        # Pages that overflow past the opener alternate verso/recto.
        story.append(NextPageTemplate(["verso", "recto"]))
        story.append(RLParagraph(escape(chapter.title), opener_title_style))
        first = True
        for block in chapter.blocks:
            if isinstance(block, Paragraph):
                style = first_body_style if first else body_style
                story.append(RLParagraph(_inline_markup(block), style))
                first = False
            elif isinstance(block, SceneBreak):
                story.append(RLParagraph("* * *", break_style))
                first = True
            elif isinstance(block, Heading):
                story.append(RLParagraph(escape(block.text), heading_style))

    for page in book.back_matter:
        story.append(NextPageTemplate("frontmatter"))
        story.append(PageBreak())
        story.append(Spacer(1, page_h * 0.4))
        for line in page.lines:
            story.append(RLParagraph(escape(line), fm_style))

    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer,
        pagesize=(page_w, page_h),
        title=title,
        author=author,
    )
    doc.addPageTemplates(templates)
    doc.build(story)
    return buffer.getvalue()


def write_pdf(
    project: WritingProject,
    allow_incomplete: bool = False,
    dest: Path | None = None,
    manifest: ShipManifest | None = None,
) -> tuple[Path, int]:
    """Gate, assemble, and write the PDF; ledger `ship.pdf`. Returns
    (path, chapter_count). Raises ShipError on blockers or a missing extra.
    A pre-built `manifest` skips the gate (used by `ship all`)."""
    if manifest is None:
        manifest = require_ready(project, allow_incomplete)
    book = assemble_book(project, manifest)
    data = build_pdf_bytes(book, trim=project.config.ship.trim)
    out = dest or project.resolve(f"export/{manifest.slug}.pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    Ledger(project.root).append(
        "ship.pdf", target=str(out.relative_to(project.root)), chapters=len(book.chapters)
    )
    return out, len(book.chapters)
