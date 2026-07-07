"""Memory: rolling chapter summaries backed by `.stoner/memory.json`.

The agent never reads whole manuscripts into context (see
`docs/planning/ARCHITECTURE.md`); instead it gets canon plus these rolling
summaries. Schema::

    {
      "book_so_far": "<condensed running narrative>",
      "chapters": {
        "5": {
          "summary": "...",
          "pov": "...",
          "words": 1200,
          "new_facts": [...],
          "updated_at": 1730000000.0
        }
      }
    }

Chapter keys are strings because JSON object keys must be strings.
"""

from __future__ import annotations

import time
from typing import Any

from ..project import WritingProject

_DEFAULT_BOOK_SO_FAR_CAP = 6000
_RECENT_WINDOW = 3


class Memory:
    """Read/write helper over `WritingProject.read_memory`/`write_memory`."""

    def __init__(self, project: WritingProject):
        self.project = project

    # -- raw access -------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        data = self.project.read_memory()
        data.setdefault("book_so_far", "")
        data.setdefault("chapters", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.project.write_memory(data)

    # -- chapters -----------------------------------------------------------

    def get_chapter_summary(self, number: int) -> str | None:
        chapter = self._load()["chapters"].get(str(number))
        return chapter.get("summary") if chapter else None

    def get_chapter(self, number: int) -> dict[str, Any] | None:
        return self._load()["chapters"].get(str(number))

    def set_chapter_summary(
        self,
        number: int,
        summary: str,
        pov: str = "",
        words: int = 0,
        new_facts: list[Any] | None = None,
    ) -> None:
        data = self._load()
        data["chapters"][str(number)] = {
            "summary": summary,
            "pov": pov,
            "words": words,
            "new_facts": new_facts or [],
            "updated_at": time.time(),
        }
        self._save(data)

    # -- book so far --------------------------------------------------------

    @property
    def book_so_far(self) -> str:
        return self._load().get("book_so_far", "")

    def rebuild_book_so_far(self, max_chars: int = _DEFAULT_BOOK_SO_FAR_CAP) -> str:
        """Concatenate chapter summaries in chapter order, capped in length.

        If the concatenation exceeds `max_chars`, the oldest chapters are
        dropped first so the most recent story state survives -- the tail
        of the book matters more to "what's true right now" than the
        opening chapters, which are also the ones most reflected in canon
        already.
        """
        data = self._load()
        chapters = data["chapters"]
        numbers = sorted((int(n) for n in chapters), reverse=True)
        lines: list[str] = []
        total = 0
        for n in numbers:
            summary = chapters[str(n)].get("summary", "")
            if not summary:
                continue
            line = f"Ch {n}: {summary}"
            if total + len(line) + 1 > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
        lines.reverse()
        book_so_far = "\n".join(lines)
        data["book_so_far"] = book_so_far
        self._save(data)
        return book_so_far

    # -- context assembly -----------------------------------------------------

    def context_for_chapter(self, number: int) -> str:
        """Book-so-far, full summaries for the last few chapters, and a
        compressed one-liner for everything earlier still in memory.
        """
        data = self._load()
        chapters = data["chapters"]
        sections: list[str] = []

        book_so_far = data.get("book_so_far", "")
        if book_so_far:
            sections.append(f"## Book So Far\n\n{book_so_far}")

        recent_start = max(1, number - _RECENT_WINDOW)
        recent_lines = []
        for n in range(recent_start, number):
            chapter = chapters.get(str(n))
            if chapter and chapter.get("summary"):
                pov = f" ({chapter['pov']})" if chapter.get("pov") else ""
                recent_lines.append(f"### Chapter {n}{pov}\n\n{chapter['summary']}")
        if recent_lines:
            sections.append("## Recent Chapters\n\n" + "\n\n".join(recent_lines))

        earlier_lines = []
        for n in sorted(int(k) for k in chapters):
            if n >= recent_start:
                continue
            summary = chapters[str(n)].get("summary", "")
            if summary:
                earlier_lines.append(f"- Ch {n}: {_compress(summary)}")
        if earlier_lines:
            sections.append("## Earlier Chapters (compressed)\n\n" + "\n".join(earlier_lines))

        return "\n\n".join(sections)


def _compress(text: str, limit: int = 140) -> str:
    """One-line, length-capped compression of a chapter summary."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if len(first_line) <= limit:
        return first_line
    return first_line[: limit - 1].rstrip() + "…"
