"""CanonStore: read/query/update the canon/ directory.

Canon is the single source of truth described in
`docs/planning/ARCHITECTURE.md`: `premise.md`, `style.md`,
`canon/characters/<slug>.md`, `canon/world/<slug>.md`, `timeline.md`,
`threads.md`. This module is the only place that parses or rewrites those
files -- the engine tools, review passes, and archivist all go through it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from ..project import WritingProject, join_frontmatter, split_frontmatter

CanonKind = Literal[
    "premise", "style", "character", "world", "timeline", "threads", "motifs",
    "fact", "other",
]

#: The four species of promise a thread row can carry in its `kind` cell. An
#: empty `kind` marks a plain plot thread; a non-empty one marks a promise to
#: the reader (a mystery to answer, a threat to discharge, a want to satisfy,
#: or an image to pay off).
PROMISE_KINDS = ("mystery", "threat", "want", "image")

_TEMPLATE_STEM = "_template"


class CanonError(RuntimeError):
    """Raised for malformed canon files or invalid store operations."""


def slugify(name: str) -> str:
    """Turn a display name into the slug convention used for filenames."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return slug.strip("-") or "unnamed"


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


@dataclass
class CanonEntry:
    """One parsed canon file."""

    rel_path: str
    kind: CanonKind
    name: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def slug(self) -> str:
        return self.rel_path.rsplit("/", 1)[-1].removesuffix(".md")


@dataclass
class SearchHit:
    """A canon entry that matched a search query, with the matching lines."""

    rel_path: str
    kind: CanonKind
    name: str
    lines: list[str] = field(default_factory=list)  # "12: ...matching line..."


@dataclass
class ThreadRow:
    id: str
    thread: str
    opened_in: str = ""
    status: str = "open"
    resolved_in: str = ""
    notes: str = ""
    kind: str = ""  # "" plain thread; else a PROMISE_KINDS value (a promise)


@dataclass
class TimelineRow:
    when: str
    event: str
    chapters: str = ""
    characters: str = ""


@dataclass
class MotifRow:
    id: str
    motif: str
    anchors: str = ""  # semicolon-separated anchor phrases within the cell
    meaning: str = ""
    notes: str = ""

    def anchor_list(self) -> list[str]:
        """Split the anchors cell into stripped, non-empty anchor phrases."""
        return [a.strip() for a in self.anchors.split(";") if a.strip()]


# ---------------------------------------------------------------------------
# Markdown table helpers
# ---------------------------------------------------------------------------


def _split_row(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c or "---") for c in cells)


def _find_table(text: str) -> tuple[int, int] | None:
    """Return (start_line, end_line_exclusive) of the first pipe-table block."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("|"):
            start = i
            break
    if start is None:
        return None
    end = start
    while end < len(lines) and lines[end].strip().startswith("|"):
        end += 1
    return start, end


def parse_table(text: str) -> tuple[str, list[str], list[list[str]], str]:
    """Parse the first markdown pipe-table in `text`.

    Returns (prefix, headers, rows, suffix) where prefix/suffix are the raw
    text before/after the table block, preserved verbatim on rewrite.
    """
    lines = text.splitlines()
    span = _find_table(text)
    if span is None:
        raise CanonError("no markdown table found")
    start, end = span
    prefix = "\n".join(lines[:start])
    suffix = "\n".join(lines[end:])
    header = _split_row(lines[start])
    body_lines = lines[start + 1 : end]
    if body_lines and _is_separator_row(_split_row(body_lines[0])):
        body_lines = body_lines[1:]
    rows = [_split_row(row_line) for row_line in body_lines if row_line.strip()]
    return prefix, header, rows, suffix


def render_table(prefix: str, headers: list[str], rows: list[list[str]], suffix: str) -> str:
    out = [] if not prefix.strip() else [prefix.rstrip("\n"), ""]
    out.append("| " + " | ".join(headers) + " |")
    out.append("|" + "|".join("-" * max(len(h), 3) for h in headers) + "|")
    for row in rows:
        cells = list(row) + [""] * (len(headers) - len(row))
        out.append("| " + " | ".join(cells[: len(headers)]) + " |")
    text = "\n".join(out)
    if suffix.strip():
        text += "\n\n" + suffix.strip("\n")
    return text + "\n"


def _chapter_num(value: Any) -> int:
    """Best-effort numeric chapter reference from strings like 'ch-05'."""
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else -1


def extract_section(body: str, heading: str) -> str:
    """Grab the content of a `## heading` section up to the next `## `."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s+|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(body)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class CanonStore:
    """Read/query/update the `canon/` directory of a `WritingProject`."""

    def __init__(self, project: WritingProject):
        self.project = project

    # -- generic entry access -------------------------------------------

    def _entry_kind(self, rel_path: str) -> CanonKind:
        if rel_path == "canon/premise.md":
            return "premise"
        if rel_path == "canon/style.md":
            return "style"
        if rel_path == "canon/timeline.md":
            return "timeline"
        if rel_path == "canon/threads.md":
            return "threads"
        if rel_path == "canon/motifs.md":
            return "motifs"
        if rel_path.startswith("canon/characters/"):
            return "character"
        if rel_path.startswith("canon/world/"):
            return "world"
        if rel_path.startswith("canon/facts/"):
            return "fact"
        return "other"

    def get(self, rel_path: str) -> CanonEntry | None:
        try:
            raw = self.project.read(rel_path)
        except Exception:
            return None
        fm, body = split_frontmatter(raw)
        kind = self._entry_kind(rel_path)
        name = str(fm.get("name") or "") or rel_path.rsplit("/", 1)[-1].removesuffix(".md")
        return CanonEntry(rel_path=rel_path, kind=kind, name=name, frontmatter=fm, body=body)

    def list_entries(self, kind: CanonKind | None = None) -> list[CanonEntry]:
        canon_dir = self.project.root / "canon"
        if not canon_dir.exists():
            return []
        entries: list[CanonEntry] = []
        for path in sorted(canon_dir.rglob("*.md")):
            if path.stem == _TEMPLATE_STEM:
                continue
            rel = str(path.relative_to(self.project.root)).replace("\\", "/")
            entry = self.get(rel)
            if entry is not None:
                entries.append(entry)
        if kind is not None:
            entries = [e for e in entries if e.kind == kind]
        return entries

    def get_character(self, slug: str) -> CanonEntry | None:
        return self.get(f"canon/characters/{slug}.md")

    def get_world(self, slug: str) -> CanonEntry | None:
        return self.get(f"canon/world/{slug}.md")

    def get_fact(self, slug: str) -> CanonEntry | None:
        return self.get(f"canon/facts/{slug}.md")

    def find_character_by_name(self, name: str) -> CanonEntry | None:
        return self._find_by_name("character", name)

    def find_world_by_name(self, name: str) -> CanonEntry | None:
        return self._find_by_name("world", name)

    def _find_by_name(self, kind: CanonKind, name: str) -> CanonEntry | None:
        slug = slugify(name)
        direct = self.get(f"canon/{'characters' if kind == 'character' else 'world'}/{slug}.md")
        if direct is not None:
            return direct
        needle = name.strip().lower()
        for entry in self.list_entries(kind=kind):
            if str(entry.frontmatter.get("name", "")).strip().lower() == needle:
                return entry
        return None

    # -- search -----------------------------------------------------------

    def search(self, query: str) -> list[SearchHit]:
        needle = query.strip().lower()
        if not needle:
            return []
        hits: list[SearchHit] = []
        for entry in self.list_entries():
            raw = self.project.read(entry.rel_path)
            matches = [
                f"{i}: {line.strip()}"
                for i, line in enumerate(raw.splitlines(), start=1)
                if needle in line.lower()
            ]
            if matches:
                hits.append(
                    SearchHit(rel_path=entry.rel_path, kind=entry.kind, name=entry.name, lines=matches)
                )
        return hits

    # -- character / world upsert -----------------------------------------

    def upsert_character(
        self, slug: str, frontmatter_updates: dict[str, Any] | None = None, body: str | None = None
    ) -> CanonEntry:
        return self._upsert(
            "characters",
            "character",
            slug,
            frontmatter_updates,
            body,
            defaults={"name": slug.replace("-", " ").title(), "role": "", "status": "alive"},
        )

    def upsert_world(
        self, slug: str, frontmatter_updates: dict[str, Any] | None = None, body: str | None = None
    ) -> CanonEntry:
        return self._upsert(
            "world",
            "world",
            slug,
            frontmatter_updates,
            body,
            defaults={"name": slug.replace("-", " ").title(), "type": "place"},
        )

    def upsert_fact(
        self, slug: str, frontmatter_updates: dict[str, Any] | None = None, body: str | None = None
    ) -> CanonEntry:
        """Write (create or merge) a `canon/facts/<slug>.md` locker entry.

        Frontmatter is merged the same diff-friendly way as characters/world;
        the body (verbatim quotes and human notes) is only replaced when a
        non-None `body` is passed, so a hand-edited body survives a re-apply
        of the same slug's unchanged claim.
        """
        return self._upsert(
            "facts",
            "fact",
            slug,
            frontmatter_updates,
            body,
            defaults={
                "name": slug.replace("-", " ").title(),
                "claim": "",
                "confidence": "medium",
                "status": "unverified",
            },
        )

    def _upsert(
        self,
        dirname: str,
        kind: CanonKind,
        slug: str,
        frontmatter_updates: dict[str, Any] | None,
        body: str | None,
        defaults: dict[str, Any],
    ) -> CanonEntry:
        rel = f"canon/{dirname}/{slug}.md"
        existing = self.get(rel)
        fm: dict[str, Any] = dict(existing.frontmatter) if existing else dict(defaults)
        fm.update(frontmatter_updates or {})
        new_body = body if body is not None else (existing.body if existing else "")
        self.project.write(rel, join_frontmatter(fm, new_body))
        return CanonEntry(rel_path=rel, kind=kind, name=str(fm.get("name") or slug), frontmatter=fm, body=new_body)

    # -- style.md / banned terms -------------------------------------------

    def banned_terms(self) -> tuple[list[str], list[str]]:
        """Parse the `## Banned` fenced-yaml block from style.md. Tolerant."""
        try:
            raw = self.project.read("canon/style.md")
        except Exception:
            return [], []
        _, body = split_frontmatter(raw)
        section = extract_section(body, "Banned")
        if not section:
            return [], []
        m = re.search(r"```(?:yaml|yml)?\s*(.*?)```", section, re.DOTALL)
        if not m:
            return [], []
        try:
            data = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            return [], []
        if not isinstance(data, dict):
            return [], []
        words = [str(w) for w in (data.get("words") or [])]
        phrases = [str(p) for p in (data.get("phrases") or [])]
        return words, phrases

    def style_body_without_banned(self) -> str:
        """style.md body with the `## Banned` section stripped (for prompts)."""
        try:
            raw = self.project.read("canon/style.md")
        except Exception:
            return ""
        _, body = split_frontmatter(raw)
        return re.sub(
            r"^##\s+Banned\s*$.*?(?=^##\s+|\Z)", "", body, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL
        ).strip()

    # -- threads.md ---------------------------------------------------------

    _THREAD_COLUMNS = ("id", "thread", "opened_in", "status", "resolved_in", "notes", "kind")
    _THREAD_WIDTH = len(_THREAD_COLUMNS)

    @staticmethod
    def _thread_cells(row: ThreadRow) -> list[str]:
        return [
            row.id, row.thread, row.opened_in, row.status,
            row.resolved_in, row.notes, row.kind,
        ]

    @staticmethod
    def _with_kind_header(headers: list[str]) -> list[str]:
        """Append a trailing `kind` header to a legacy six-column threads
        table so `render_table` (which truncates rows to header width) keeps
        the new cell. Reads never call this -- only writes migrate the header.
        """
        if any(h.strip().lower() == "kind" for h in headers):
            return headers
        return [*headers, "kind"]

    def threads(self) -> list[ThreadRow]:
        try:
            raw = self.project.read("canon/threads.md")
        except Exception:
            return []
        try:
            _, _headers, rows, _ = parse_table(raw)
        except CanonError:
            return []
        w = self._THREAD_WIDTH
        return [ThreadRow(*(row + [""] * (w - len(row)))[:w]) for row in rows]

    def add_thread(
        self,
        id: str,
        thread: str,
        opened_in: str = "",
        status: str = "open",
        resolved_in: str = "",
        notes: str = "",
        kind: str = "",
    ) -> ThreadRow:
        raw = self.project.read("canon/threads.md")
        prefix, headers, rows, suffix = parse_table(raw)
        if any(row and row[0] == id for row in rows):
            raise CanonError(f"thread id already exists: {id}")
        row = ThreadRow(id, thread, opened_in, status, resolved_in, notes, kind)
        rows.append(self._thread_cells(row))
        headers = self._with_kind_header(headers)
        self.project.write("canon/threads.md", render_table(prefix, headers, rows, suffix))
        return row

    def update_thread(self, id: str, **fields: Any) -> ThreadRow:
        raw = self.project.read("canon/threads.md")
        prefix, headers, rows, suffix = parse_table(raw)
        w = self._THREAD_WIDTH
        for i, row in enumerate(rows):
            padded = (row + [""] * (w - len(row)))[:w]
            if padded[0] == id:
                current = ThreadRow(*padded)
                for key, value in fields.items():
                    if key not in self._THREAD_COLUMNS:
                        raise CanonError(f"unknown thread field: {key}")
                    setattr(current, key, value)
                rows[i] = self._thread_cells(current)
                headers = self._with_kind_header(headers)
                self.project.write("canon/threads.md", render_table(prefix, headers, rows, suffix))
                return current
        raise CanonError(f"no thread with id: {id}")

    # -- promises (typed threads) -------------------------------------------

    def promises(self) -> list[ThreadRow]:
        """Threads carrying a non-empty `kind` -- i.e. planted promises."""
        return [t for t in self.threads() if t.kind]

    def plant_promise(
        self,
        id: str,
        thread: str,
        kind: str,
        opened_in: str = "",
        notes: str = "",
    ) -> ThreadRow:
        """Plant a promise: an open thread row typed with a promise `kind`."""
        if kind not in PROMISE_KINDS:
            raise CanonError(
                f"invalid promise kind: {kind!r} (expected one of {', '.join(PROMISE_KINDS)})"
            )
        return self.add_thread(
            id, thread, opened_in=opened_in, status="open", notes=notes, kind=kind
        )

    def payoff_promise(self, id: str, resolved_in: str, notes: str = "") -> ThreadRow:
        """Pay off a promise: stamp `status=resolved` and `resolved_in`."""
        fields: dict[str, Any] = {"status": "resolved", "resolved_in": resolved_in}
        if notes:
            fields["notes"] = notes
        return self.update_thread(id, **fields)

    # -- motifs.md ----------------------------------------------------------

    _MOTIF_COLUMNS = ("id", "motif", "anchors", "meaning", "notes")
    _MOTIF_WIDTH = len(_MOTIF_COLUMNS)

    @staticmethod
    def _motif_cells(row: MotifRow) -> list[str]:
        return [row.id, row.motif, row.anchors, row.meaning, row.notes]

    def motifs(self) -> list[MotifRow]:
        try:
            raw = self.project.read("canon/motifs.md")
        except Exception:
            return []
        try:
            _, _headers, rows, _ = parse_table(raw)
        except CanonError:
            return []
        w = self._MOTIF_WIDTH
        return [MotifRow(*(row + [""] * (w - len(row)))[:w]) for row in rows]

    def add_motif(
        self,
        id: str,
        motif: str,
        anchors: str = "",
        meaning: str = "",
        notes: str = "",
    ) -> MotifRow:
        raw = self.project.read("canon/motifs.md")
        prefix, headers, rows, suffix = parse_table(raw)
        if any(row and row[0] == id for row in rows):
            raise CanonError(f"motif id already exists: {id}")
        row = MotifRow(id, motif, anchors, meaning, notes)
        rows.append(self._motif_cells(row))
        self.project.write("canon/motifs.md", render_table(prefix, headers, rows, suffix))
        return row

    def update_motif(self, id: str, **fields: Any) -> MotifRow:
        raw = self.project.read("canon/motifs.md")
        prefix, headers, rows, suffix = parse_table(raw)
        w = self._MOTIF_WIDTH
        for i, row in enumerate(rows):
            padded = (row + [""] * (w - len(row)))[:w]
            if padded[0] == id:
                current = MotifRow(*padded)
                for key, value in fields.items():
                    if key not in self._MOTIF_COLUMNS:
                        raise CanonError(f"unknown motif field: {key}")
                    setattr(current, key, value)
                rows[i] = self._motif_cells(current)
                self.project.write("canon/motifs.md", render_table(prefix, headers, rows, suffix))
                return current
        raise CanonError(f"no motif with id: {id}")

    # -- timeline.md ----------------------------------------------------------

    def timeline_rows(self) -> list[TimelineRow]:
        try:
            raw = self.project.read("canon/timeline.md")
        except Exception:
            return []
        try:
            _, _headers, rows, _ = parse_table(raw)
        except CanonError:
            return []
        return [TimelineRow(*(row + [""] * (4 - len(row)))[:4]) for row in rows]

    def add_timeline_row(
        self, when: str, event: str, chapters: str = "", characters: str = ""
    ) -> TimelineRow:
        raw = self.project.read("canon/timeline.md")
        prefix, headers, rows, suffix = parse_table(raw)
        row = TimelineRow(when, event, chapters, characters)
        rows.append([row.when, row.event, row.chapters, row.characters])
        self.project.write("canon/timeline.md", render_table(prefix, headers, rows, suffix))
        return row

    # -- context pack ---------------------------------------------------------

    def context_pack(self, max_chars: int = 12000) -> str:
        """Assemble the canon digest given to the writer agent.

        Priority order (highest first): premise, style (sans Banned),
        open threads, characters (most-recent-appearance first), world
        entries, recent timeline. Sections are added whole while budget
        allows; once budget runs out entire lower-priority units are
        dropped rather than mangled mid-section.
        """
        remaining = max_chars
        parts: list[str] = []

        def add(title: str, text: str) -> bool:
            nonlocal remaining
            text = text.strip()
            if not text:
                return True
            block = f"## {title}\n\n{text}\n"
            if len(block) <= remaining:
                parts.append(block)
                remaining -= len(block)
                return True
            if remaining > len(f"## {title}\n\n") + 20:
                budget = remaining - len(f"## {title}\n\n") - len("\n...[truncated]\n")
                parts.append(f"## {title}\n\n{text[:budget]}\n...[truncated]\n")
                remaining = 0
            return False

        premise = self.get("canon/premise.md")
        if not add("Premise", premise.body if premise else ""):
            return "".join(parts)

        if not add("Style", self.style_body_without_banned()):
            return "".join(parts)

        open_threads = [t for t in self.threads() if t.status == "open"]
        thread_text = "\n".join(f"- ({t.id}) {t.thread} -- opened {t.opened_in}" for t in open_threads)
        if not add("Open Threads", thread_text):
            return "".join(parts)

        motif_rows = self.motifs()
        motif_lines = []
        for m in motif_rows:
            anchors = "; ".join(m.anchor_list())
            suffix_a = f" (anchors: {anchors})" if anchors else ""
            motif_lines.append(f"- {m.motif}: {m.meaning}{suffix_a}".rstrip())
        if not add("Motifs", "\n".join(motif_lines)):
            return "".join(parts)

        characters = self.list_entries(kind="character")
        characters.sort(key=lambda e: _chapter_num(e.frontmatter.get("first_appearance")), reverse=True)
        char_blocks = []
        for c in characters:
            fm_yaml = yaml.safe_dump(c.frontmatter, sort_keys=False, allow_unicode=True).strip()
            voice = extract_section(c.body, "Voice")
            block = f"### {c.name}\n```yaml\n{fm_yaml}\n```"
            if voice:
                block += f"\nVoice: {voice}"
            char_blocks.append(block)
        if char_blocks:
            remaining_local = remaining - len("## Characters\n\n")
            kept = []
            for block in char_blocks:
                unit = block + "\n\n"
                if len(unit) <= remaining_local:
                    kept.append(block)
                    remaining_local -= len(unit)
                else:
                    break
            if kept and not add("Characters", "\n\n".join(kept)):
                return "".join(parts)
            elif not kept:
                return "".join(parts)

        world = self.list_entries(kind="world")
        world_blocks = []
        for w in world:
            fm_yaml = yaml.safe_dump(w.frontmatter, sort_keys=False, allow_unicode=True).strip()
            rules = extract_section(w.body, "Rules")
            block = f"### {w.name}\n```yaml\n{fm_yaml}\n```"
            if rules:
                block += f"\nRules: {rules}"
            world_blocks.append(block)
        if world_blocks:
            remaining_local = remaining - len("## World\n\n")
            kept = []
            for block in world_blocks:
                unit = block + "\n\n"
                if len(unit) <= remaining_local:
                    kept.append(block)
                    remaining_local -= len(unit)
                else:
                    break
            if kept:
                add("World", "\n\n".join(kept))

        # Facts (sourced fact locker) -- between World and Recent Timeline.
        # Sourced facts are whole-or-nothing: a fact chopped mid-line loses
        # the very specificity it exists to carry, so the section drops
        # entirely rather than truncating (unlike the prose sections above).
        from ..facts.locker import facts_digest

        facts_text = facts_digest(self)
        if facts_text:
            facts_block = f"## Facts\n\n{facts_text}\n"
            if len(facts_block) <= remaining:
                parts.append(facts_block)
                remaining -= len(facts_block)

        timeline = self.timeline_rows()[-10:]
        timeline_text = "\n".join(f"- {r.when}: {r.event} ({r.chapters})" for r in timeline)
        add("Recent Timeline", timeline_text)

        return "".join(parts)
