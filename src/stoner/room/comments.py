"""CommentStore: margin comments pinned to chapter spans.

The writer annotates any span of a chapter; the comment persists with a
response history and an open/answered/resolved/dismissed lifecycle in
`.stoner/room/comments/ch-NN.json` (one JSON file per chapter -- comments per
chapter stay small and git-friendly, assumption A4). Quotes anchor via
`review.passes.locate_span` against the *current* chapter body; a quote that
is not verbatim-present stores `span=None` plus a warning note rather than
failing, so the writer is never blocked from leaving a comment.

Every mutation appends a `room.comment.*` ledger entry. Each open comment is
an obligation the session engine must settle (see `room/session.py`).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from ..ledger import Ledger
from ..project import WritingProject
from ..review.passes import locate_span
from ..types import Span

CommentStatus = Literal["open", "answered", "resolved", "dismissed"]


class CommentResponse(BaseModel):
    editor: str  # editor slug, or "room" for the follow-up backstop
    text: str
    session: str = ""
    created_at: float = Field(default_factory=time.time)


class Comment(BaseModel):
    """One margin comment pinned (best-effort) to a chapter span."""

    id: str = Field(default_factory=lambda: f"c_{uuid.uuid4().hex[:10]}")
    chapter: int
    quote: str = ""
    span: Span | None = None
    text: str = ""
    status: CommentStatus = "open"
    note: str = ""  # e.g. an unanchored-quote warning
    responses: list[CommentResponse] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)


class CommentStore:
    """Read/write helper over `.stoner/room/comments/ch-NN.json`."""

    def __init__(self, project: WritingProject):
        self.project = project
        self.ledger = Ledger(project.root)

    def _rel(self, chapter: int) -> str:
        return f".stoner/room/comments/ch-{chapter:02d}.json"

    def _load(self, chapter: int) -> list[Comment]:
        path = self.project.resolve(self._rel(chapter))
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return []  # malformed comment file degrades to empty
        if not isinstance(raw, list):
            return []
        out: list[Comment] = []
        for item in raw:
            try:
                out.append(Comment.model_validate(item))
            except Exception:  # noqa: BLE001 - skip an unparseable comment, keep the rest
                continue
        return out

    def _save(self, chapter: int, comments: list[Comment]) -> None:
        path = self.project.resolve(self._rel(chapter))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [c.model_dump(mode="json") for c in comments]
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- reads ------------------------------------------------------------

    def list(self, chapter: int, only_open: bool = False) -> list[Comment]:
        comments = self._load(chapter)
        if only_open:
            return [c for c in comments if c.status == "open"]
        return comments

    def get(self, chapter: int, comment_id: str) -> Comment | None:
        for c in self._load(chapter):
            if c.id == comment_id:
                return c
        return None

    # -- writes -----------------------------------------------------------

    def add(self, chapter: int, quote: str, text: str) -> Comment:
        """Add a comment, anchoring `quote` against the current chapter body.

        An absent/empty quote stores `span=None` with a warning note; the
        comment is still created (never blocked)."""
        span: Span | None = None
        note = ""
        try:
            _fm, body = self.project.read_chapter(chapter)
        except Exception:  # noqa: BLE001 - a missing chapter still accepts a comment, unanchored
            body = ""
            note = f"chapter ch-{chapter:02d} not found; comment stored without a span"
        if quote and body:
            span = locate_span(body, quote)
            if span is None:
                note = "quote not found verbatim in the chapter; comment stored without a span"
        elif quote and not body:
            pass  # note already set above
        comment = Comment(chapter=chapter, quote=quote, text=text, span=span, note=note)
        comments = self._load(chapter)
        comments.append(comment)
        self._save(chapter, comments)
        self.ledger.append(
            "room.comment.add",
            target=self._rel(chapter),
            comment_id=comment.id,
            anchored=span is not None,
        )
        return comment

    def append_response(
        self, chapter: int, comment_id: str, editor: str, text: str, session: str = ""
    ) -> Comment | None:
        """Attach an editor's response and flip an open comment to answered."""
        comments = self._load(chapter)
        for c in comments:
            if c.id == comment_id:
                c.responses.append(CommentResponse(editor=editor, text=text, session=session))
                if c.status == "open":
                    c.status = "answered"
                self._save(chapter, comments)
                self.ledger.append(
                    "room.comment.answer",
                    target=self._rel(chapter),
                    comment_id=comment_id,
                    editor=editor,
                    session=session,
                )
                return c
        return None

    def set_status(self, chapter: int, comment_id: str, status: CommentStatus) -> Comment | None:
        comments = self._load(chapter)
        for c in comments:
            if c.id == comment_id:
                c.status = status
                self._save(chapter, comments)
                self.ledger.append(
                    "room.comment.resolve",
                    target=self._rel(chapter),
                    comment_id=comment_id,
                    status=status,
                )
                return c
        return None
