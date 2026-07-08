"""Notebook: persistent, capped, per-editor memory for the Writers' Room.

Mirrors `canon/memory.py:Memory` in shape and discipline: a JSON file under
`.stoner/room/notebooks/<slug>.json`, `setdefault`-based schema tolerance, a
corrupt file degrading to an empty notebook rather than raising, and hard caps
so the notebook can never grow unbounded into prompt context (the
`rebuild_book_so_far` cap-and-drop-oldest idea applied to tracked items).

Schema::

    {
      "editor": "<slug>",
      "opinion": "<running one-paragraph take, char-capped>",
      "items": [
        {
          "id": "<finding id -- items key off finding ids>",
          "chapter": 5,
          "quote": "<verbatim offending text>",
          "issue": "<what is wrong>",
          "severity": "minor",
          "pass": "line",
          "status": "open|persisting|resolved|dismissed|unlocatable",
          "first_session": "<session id>",
          "last_seen_session": "<session id>",
          "escalations": 0
        }
      ],
      "updated_at": 1730000000.0
    }

Item status is the editor's *own* cross-session bookkeeping and is distinct
from `Finding.status` (the human-triage lifecycle). An LLM-judged `resolved`
never overwrites a human triage; a human `dismissed` on a room finding flows
the other way with full authority via `reconcile_dismissals` -- triage
silences the editor's memory (no re-location, no digest, no escalation), it
does not merely stop escalation.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..config import RoomConfig
from ..project import WritingProject

# Statuses that still track (re-located, escalated, shown in the digest).
_ACTIVE = ("open", "persisting", "unlocatable")


class Notebook:
    """Read/write helper over one editor's `.stoner/room/notebooks/<slug>.json`."""

    def __init__(self, project: WritingProject, slug: str, config: RoomConfig | None = None):
        self.project = project
        self.slug = slug
        self.config = config or RoomConfig()
        self.rel = f".stoner/room/notebooks/{slug}.json"

    # -- raw access -------------------------------------------------------

    def _path(self):
        return self.project.resolve(self.rel)

    def _load(self) -> dict[str, Any]:
        path = self._path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                data = {}  # corrupt notebook degrades to empty rather than raising
        data.setdefault("editor", self.slug)
        data.setdefault("opinion", "")
        data.setdefault("items", [])
        if not isinstance(data["items"], list):
            data["items"] = []
        return data

    def _save(self, data: dict[str, Any]) -> None:
        data["updated_at"] = time.time()
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- accessors --------------------------------------------------------

    @property
    def opinion(self) -> str:
        return self._load().get("opinion", "")

    def items(self) -> list[dict[str, Any]]:
        return list(self._load()["items"])

    def tracked_items(self, chapter: int | None = None) -> list[dict[str, Any]]:
        """Active items (open/persisting/unlocatable), optionally scoped to a
        chapter -- the set re-located at session start and shown in digests."""
        out = [it for it in self._load()["items"] if it.get("status") in _ACTIVE]
        if chapter is not None:
            out = [it for it in out if it.get("chapter") == chapter]
        return out

    def get(self, item_id: str) -> dict[str, Any] | None:
        for it in self._load()["items"]:
            if it.get("id") == item_id:
                return it
        return None

    # -- mutation ---------------------------------------------------------

    def upsert_item(
        self,
        finding_id: str,
        chapter: int,
        quote: str,
        issue: str,
        severity: str,
        pass_name: str,
        session_id: str,
    ) -> None:
        """Insert a new tracked item or refresh an existing one (keyed by
        finding id). New items start `open`; existing items keep their status
        and escalation count and just bump `last_seen_session`."""
        data = self._load()
        for it in data["items"]:
            if it.get("id") == finding_id:
                it["last_seen_session"] = session_id
                it["quote"] = quote
                it["issue"] = issue
                it["severity"] = severity
                self._save(data)
                return
        data["items"].append(
            {
                "id": finding_id,
                "chapter": chapter,
                "quote": quote,
                "issue": issue,
                "severity": severity,
                "pass": pass_name,
                "status": "open",
                "first_session": session_id,
                "last_seen_session": session_id,
                "escalations": 0,
            }
        )
        self._save(data)

    def mark_status(self, item_id: str, status: str, session_id: str | None = None) -> None:
        data = self._load()
        for it in data["items"]:
            if it.get("id") == item_id:
                it["status"] = status
                if session_id:
                    it["last_seen_session"] = session_id
                self._save(data)
                return

    def mark_persisting(self, item_id: str, session_id: str, new_quote: str | None = None) -> None:
        """A prior flag whose text is unchanged: escalate on the record.

        Escalation is record-keeping only -- the count rises and the framing
        gets louder, but severity is never mutated (invariant 2)."""
        data = self._load()
        for it in data["items"]:
            if it.get("id") == item_id:
                it["status"] = "persisting"
                it["escalations"] = int(it.get("escalations", 0)) + 1
                it["last_seen_session"] = session_id
                if new_quote:
                    it["quote"] = new_quote
                self._save(data)
                return

    def set_opinion(self, text: str) -> None:
        """Replace the running opinion, capped at `opinion_cap_chars`."""
        cap = self.config.opinion_cap_chars
        text = (text or "").strip()
        if len(text) > cap:
            text = text[: cap - 1].rstrip() + "…"
        data = self._load()
        data["opinion"] = text
        self._save(data)

    def reconcile_dismissals(self, session_records: list[dict[str, Any]]) -> int:
        """Fold human `dismissed` triage on room findings into the notebook.

        Scans the given session records for findings this editor owns
        (source `room:<slug>...`) whose status a human set to `dismissed`, and
        marks the matching tracked item `dismissed`. Idempotent: an item
        already dismissed is skipped. Returns the number newly dismissed."""
        data = self._load()
        by_id = {it["id"]: it for it in data["items"] if "id" in it}
        prefix = f"room:{self.slug}"
        changed = 0
        for rec in session_records:
            for f in rec.get("findings", []) or []:
                if not isinstance(f, dict) or f.get("status") != "dismissed":
                    continue
                if not str(f.get("source", "")).startswith(prefix):
                    continue
                it = by_id.get(f.get("id"))
                if it is not None and it.get("status") != "dismissed":
                    it["status"] = "dismissed"
                    changed += 1
        if changed:
            self._save(data)
        return changed

    def evict(self) -> list[str]:
        """Enforce caps: drop oldest resolved beyond `max_resolved_items`,
        then oldest low-severity active beyond `max_open_items`. Records an
        eviction note in the opinion. Returns dropped item ids."""
        data = self._load()
        items = data["items"]
        dropped: list[str] = []

        def _order_key(it: dict[str, Any]) -> Any:
            # Oldest first by session recency proxy; ids sort stably as a tiebreak.
            return (str(it.get("last_seen_session", "")), str(it.get("id", "")))

        resolved = [it for it in items if it.get("status") == "resolved"]
        if len(resolved) > self.config.max_resolved_items:
            excess = sorted(resolved, key=_order_key)[: len(resolved) - self.config.max_resolved_items]
            drop_ids = {it["id"] for it in excess}
            dropped.extend(sorted(drop_ids))
            items = [it for it in items if it.get("id") not in drop_ids]

        active = [it for it in items if it.get("status") in _ACTIVE]
        if len(active) > self.config.max_open_items:
            # Prefer to keep higher-severity active items; drop lowest first.
            sev_rank = {"critical": 0, "major": 1, "minor": 2, "info": 3}
            low_first = sorted(
                active,
                key=lambda it: (-sev_rank.get(str(it.get("severity", "minor")), 2), _order_key(it)),
            )
            excess = low_first[: len(active) - self.config.max_open_items]
            drop_ids = {it["id"] for it in excess}
            dropped.extend(sorted(drop_ids))
            items = [it for it in items if it.get("id") not in drop_ids]

        if dropped:
            data["items"] = items
            note = f"[evicted {len(dropped)} old item(s) to stay under cap]"
            opinion = data.get("opinion", "")
            data["opinion"] = (opinion + ("\n" if opinion else "") + note)[: self.config.opinion_cap_chars]
            self._save(data)
        return dropped

    # -- prompt injection -------------------------------------------------

    def digest(self, chapter: int | None = None, max_chars: int | None = None) -> str:
        """Prompt-ready view: opinion plus active items (most recent first),
        scope-filtered and truncated at `max_chars` (default digest_chars)."""
        cap = max_chars if max_chars is not None else self.config.digest_chars
        data = self._load()
        sections: list[str] = []
        opinion = data.get("opinion", "").strip()
        if opinion:
            sections.append(f"### Running opinion\n\n{opinion}")

        active = [it for it in data["items"] if it.get("status") in _ACTIVE]
        if chapter is not None:
            active = [it for it in active if it.get("chapter") == chapter]
        active.sort(key=lambda it: str(it.get("last_seen_session", "")), reverse=True)

        lines: list[str] = []
        for it in active:
            esc = int(it.get("escalations", 0))
            tag = f" (flagged before, persists x{esc})" if esc else ""
            ch = it.get("chapter")
            quote = str(it.get("quote", ""))
            quote = quote if len(quote) <= 120 else quote[:119] + "…"
            lines.append(
                f"- [ch {ch}] {it.get('status')}{tag}: {it.get('issue', '')}"
                + (f" — “{quote}”" if quote else "")
            )
        if lines:
            sections.append("### Open items\n\n" + "\n".join(lines))

        text = "\n\n".join(sections)
        if len(text) > cap:
            text = text[: cap - 1].rstrip() + "…"
        return text
