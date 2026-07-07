"""Deterministic chapter renumbering: plan, apply, and integrity checks.

Two layers, mirroring `canon/archivist.py`'s plan-then-apply split:
`plan_renumber` computes every rename and cell edit as data without touching
disk; `apply_renumber` executes the plan in collision-safe order (renames go
through a temp suffix so overlapping mappings never clobber), snapshotting
every affected manuscript body under its original number BEFORE any file
moves. `check_integrity` is the deterministic checker that gates refactors
and backs `stoner drafts verify`: it is free and reproducible, so refactor
commands treat any `major` problem as a command failure, while model-based
verification stays advisory (see `archaeology/verify.py`).

Blast radius covered: manuscript files, `outline/beats/`, `.stoner/drafts/`
directories, `.stoner/memory.json` chapter keys, `.stoner/book-state.json`,
and chapter-reference cells in `canon/threads.md` / `canon/timeline.md`
(renumbered only when the cell parses as a chapter reference -- bare int,
`ch N`, `ch-NN` forms -- otherwise flagged, never guessed).
`.stoner/reviews/` filenames are timestamped history and are never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..canon.memory import Memory
from ..canon.store import CanonError, parse_table, render_table
from ..ledger import Ledger
from ..project import CHAPTER_RE, WritingProject, split_frontmatter
from ..types import Finding, Severity
from .snapshots import DraftStore, body_hash

INTEGRITY_SOURCE = "drafts:integrity"

_TMP_SUFFIX = ".renumber-tmp"
_CHAPTER_REF_RE = re.compile(r"^(ch(?:apter)?[-\s]?)?(\d+)$", re.IGNORECASE)

# threads.md / timeline.md chapter-reference column indexes
_THREADS_CHAPTER_COLS = (2, 4)  # opened_in, resolved_in
_TIMELINE_CHAPTER_COLS = (2,)  # chapters


# ---------------------------------------------------------------------------
# Chapter-reference cell parsing
# ---------------------------------------------------------------------------


def parse_chapter_ref(token: str) -> int | None:
    """Chapter number from a single-ref token, or None when it isn't one."""
    m = _CHAPTER_REF_RE.match(token.strip())
    return int(m.group(2)) if m else None


def _renumber_token(token: str, mapping: dict[int, int]) -> str:
    """Rewrite one parseable ref token, preserving prefix and zero-padding."""
    m = _CHAPTER_REF_RE.match(token.strip())
    assert m is not None
    prefix = m.group(1) or ""
    digits = m.group(2)
    n = int(digits)
    new = mapping.get(n, n)
    return f"{prefix}{new:0{len(digits)}d}"


def renumber_cell(cell: str, mapping: dict[int, int]) -> str | None:
    """Renumber a table cell of comma-separated chapter refs.

    Returns the rewritten cell, or None when any part does not parse as a
    chapter reference (the caller flags instead of guessing).
    """
    parts = [p.strip() for p in cell.split(",")]
    if any(parse_chapter_ref(p) is None for p in parts if p) or not any(parts):
        return None
    return ", ".join(_renumber_token(p, mapping) for p in parts if p)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class CellEdit:
    """One table-cell rewrite in threads.md or timeline.md."""

    row: int  # 0-based row index into the parsed table body
    col: int
    old: str
    new: str


@dataclass
class RenumberPlan:
    """Every rename/rewrite a mapping implies, computed without touching disk."""

    mapping: dict[int, int]
    manuscript_moves: list[tuple[str, str]] = field(default_factory=list)
    beats_moves: list[tuple[str, str]] = field(default_factory=list)
    drafts_moves: list[tuple[str, str]] = field(default_factory=list)
    memory_moves: dict[int, int] = field(default_factory=dict)
    book_state_moves: dict[int, int] = field(default_factory=dict)
    threads_edits: list[CellEdit] = field(default_factory=list)
    timeline_edits: list[CellEdit] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _beats_rel(number: int) -> str:
    return f"outline/beats/ch-{number:02d}.md"


def _drafts_rel(number: int) -> str:
    return f".stoner/drafts/ch-{number:02d}"


def _plan_table_edits(
    raw: str, name: str, cols: tuple[int, ...], mapping: dict[int, int], plan: RenumberPlan
) -> list[CellEdit]:
    edits: list[CellEdit] = []
    try:
        _prefix, _headers, rows, _suffix = parse_table(raw)
    except CanonError:
        return edits
    for r, row in enumerate(rows):
        for c in cols:
            cell = row[c].strip() if c < len(row) else ""
            if not cell:
                continue
            new = renumber_cell(cell, mapping)
            if new is None:
                plan.flags.append(
                    f"{name} row {r + 1} col {c + 1}: {cell!r} is not a chapter "
                    "reference — renumber by hand if it refers to a chapter"
                )
            elif new != cell:
                edits.append(CellEdit(row=r, col=c, old=cell, new=new))
    return edits


def plan_renumber(project: WritingProject, mapping: dict[int, int]) -> RenumberPlan:
    """Compute every rename/rewrite `mapping` (old -> new) implies, as data."""
    plan = RenumberPlan(mapping=dict(mapping))
    for old, new in sorted(mapping.items()):
        if old == new:
            continue
        if (project.root / project.chapter_rel(old)).exists():
            plan.manuscript_moves.append((project.chapter_rel(old), project.chapter_rel(new)))
            # A snapshot taken during apply may create the drafts dir even
            # when it does not exist yet, so plan its move alongside.
            if (_drafts_rel(old), _drafts_rel(new)) not in plan.drafts_moves:
                plan.drafts_moves.append((_drafts_rel(old), _drafts_rel(new)))
        elif (project.root / _drafts_rel(old)).exists():
            plan.drafts_moves.append((_drafts_rel(old), _drafts_rel(new)))
        if (project.root / _beats_rel(old)).exists():
            plan.beats_moves.append((_beats_rel(old), _beats_rel(new)))

    memory = project.read_memory()
    for key in memory.get("chapters", {}):
        try:
            n = int(key)
        except ValueError:
            continue
        if n in mapping and mapping[n] != n:
            plan.memory_moves[n] = mapping[n]

    state_path = project.root / ".stoner" / "book-state.json"
    if state_path.exists():
        from ..pipelines.book import load_state

        state = load_state(project)
        touched = set(state.chapters_done) | set(state.chapters_planned)
        for n in sorted(touched):
            if n in mapping and mapping[n] != n:
                plan.book_state_moves[n] = mapping[n]

    for rel, cols, bucket in (
        ("canon/threads.md", _THREADS_CHAPTER_COLS, "threads"),
        ("canon/timeline.md", _TIMELINE_CHAPTER_COLS, "timeline"),
    ):
        path = project.root / rel
        if not path.exists():
            continue
        edits = _plan_table_edits(
            path.read_text(encoding="utf-8"), rel, cols, mapping, plan
        )
        if bucket == "threads":
            plan.threads_edits = edits
        else:
            plan.timeline_edits = edits

    return plan


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _snapshot_before_move(
    project: WritingProject, rel: str, reason: str, ledger: Ledger
) -> None:
    """Preserve a manuscript body under its original number before renaming."""
    m = re.match(r"manuscript/ch-(\d+)\.md$", rel)
    if m is None:
        return
    number = int(m.group(1))
    full_text = project.read(rel)
    _fm, body = split_frontmatter(full_text)
    store = DraftStore(project)
    manifest = store.load_manifest(number)
    entry = store.record(
        number,
        manifest,
        full_text,
        body_hash(body),
        reason,
        detail={"renumber": True},
        result_sha256=body_hash(body),
    )
    store.save_manifest(number, manifest)
    ledger.append(
        "drafts.snapshot", target=rel, chapter=number, seq=entry.seq, reason=reason
    )


def _apply_table_edits(project: WritingProject, rel: str, edits: list[CellEdit]) -> None:
    if not edits:
        return
    raw = project.read(rel)
    prefix, headers, rows, suffix = parse_table(raw)
    for e in edits:
        while len(rows[e.row]) <= e.col:
            rows[e.row].append("")
        rows[e.row][e.col] = e.new
    project.write(rel, render_table(prefix, headers, rows, suffix))


def apply_renumber(
    project: WritingProject, plan: RenumberPlan, reason: str = "renumber"
) -> None:
    """Execute a RenumberPlan: snapshot-first, collision-safe renames, then
    memory / book-state / canon-table rewrites. Raises `RuntimeError` on a
    destination collision (nothing in the colliding pair is overwritten)."""
    ledger = Ledger(project.root)

    for old_rel, _new_rel in plan.manuscript_moves:
        _snapshot_before_move(project, old_rel, reason, ledger)

    moves = plan.manuscript_moves + plan.beats_moves + plan.drafts_moves
    # Phase 1: vacate every source through a temp name so overlapping
    # mappings (2->3, 3->4) never clobber each other.
    staged: list[tuple[Path, Path]] = []
    for old_rel, new_rel in moves:
        src = project.root / old_rel
        if not src.exists():
            continue
        tmp = src.parent / (src.name + _TMP_SUFFIX)
        src.rename(tmp)
        staged.append((tmp, project.root / new_rel))
    # Phase 2: land every temp at its destination.
    for tmp, dst in staged:
        if dst.exists():
            raise RuntimeError(
                f"renumber collision: {dst.relative_to(project.root)} already "
                f"exists — staged file left at {tmp.relative_to(project.root)}"
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(dst)

    if plan.memory_moves:
        data = project.read_memory()
        chapters = data.get("chapters", {})
        remapped = {}
        for key, value in chapters.items():
            try:
                n = int(key)
            except ValueError:
                remapped[key] = value
                continue
            remapped[str(plan.memory_moves.get(n, n))] = value
        data["chapters"] = remapped
        project.write_memory(data)
        Memory(project).rebuild_book_so_far()

    if plan.book_state_moves:
        from ..pipelines.book import load_state, save_state

        state = load_state(project)
        state.chapters_planned = sorted(
            plan.book_state_moves.get(n, n) for n in state.chapters_planned
        )
        state.chapters_done = {
            plan.book_state_moves.get(n, n): v for n, v in state.chapters_done.items()
        }
        save_state(project, state)

    _apply_table_edits(project, "canon/threads.md", plan.threads_edits)
    _apply_table_edits(project, "canon/timeline.md", plan.timeline_edits)


# ---------------------------------------------------------------------------
# Integrity checks (deterministic; `major` findings gate refactors)
# ---------------------------------------------------------------------------


def _finding(severity: Severity, category: str, issue: str) -> Finding:
    return Finding(source=INTEGRITY_SOURCE, severity=severity, category=category, issue=issue)


def check_integrity(project: WritingProject) -> list[Finding]:
    """Deterministic project-consistency problems, as Findings.

    `major` findings are broken references a renumber should never leave
    behind; `minor`/`info` are advisory (missing beat sheets, hand-edits,
    unresolvable table cells)."""
    findings: list[Finding] = []
    numbers = sorted(c.number for c in project.chapters())

    if numbers:
        expected = list(range(numbers[0], numbers[0] + len(numbers)))
        if numbers != expected:
            findings.append(
                _finding(
                    Severity.major,
                    "chapter-gap",
                    f"non-contiguous chapter numbers: {numbers}",
                )
            )

    have = set(numbers)
    beats_dir = project.root / "outline" / "beats"
    beats_nums: set[int] = set()
    if beats_dir.exists():
        for f in sorted(beats_dir.iterdir()):
            m = CHAPTER_RE.match(f.name)
            if m:
                beats_nums.add(int(m.group(1)))
    for n in sorted(beats_nums - have):
        findings.append(
            _finding(
                Severity.major,
                "orphan-beats",
                f"outline/beats/ch-{n:02d}.md has no manuscript chapter",
            )
        )
    for n in sorted(have - beats_nums):
        findings.append(
            _finding(Severity.info, "missing-beats", f"ch-{n:02d} has no beat sheet")
        )

    memory = project.read_memory()
    for key in sorted(memory.get("chapters", {})):
        try:
            n = int(key)
        except ValueError:
            continue
        if n not in have:
            findings.append(
                _finding(
                    Severity.minor,
                    "orphan-memory",
                    f"memory.json has a summary for missing ch-{n:02d}",
                )
            )

    state_path = project.root / ".stoner" / "book-state.json"
    if state_path.exists():
        from ..pipelines.book import load_state

        state = load_state(project)
        for n in sorted(set(state.chapters_done) - have):
            findings.append(
                _finding(
                    Severity.major,
                    "book-state",
                    f"book-state.json marks ch-{n:02d} done but the chapter is missing",
                )
            )

    for rel, cols in (
        ("canon/threads.md", _THREADS_CHAPTER_COLS),
        ("canon/timeline.md", _TIMELINE_CHAPTER_COLS),
    ):
        path = project.root / rel
        if not path.exists():
            continue
        try:
            _p, _h, rows, _s = parse_table(path.read_text(encoding="utf-8"))
        except CanonError:
            continue
        for r, row in enumerate(rows):
            for c in cols:
                cell = row[c].strip() if c < len(row) else ""
                if not cell:
                    continue
                parts = [p.strip() for p in cell.split(",") if p.strip()]
                refs = [parse_chapter_ref(p) for p in parts]
                if any(ref is None for ref in refs):
                    findings.append(
                        _finding(
                            Severity.minor,
                            "table-ref",
                            f"{rel} row {r + 1}: {cell!r} is not a chapter reference",
                        )
                    )
                    continue
                for ref in refs:
                    if ref is not None and ref not in have:
                        # minor, not major: threads/timeline legitimately
                        # reference planned-but-unwritten chapters.
                        findings.append(
                            _finding(
                                Severity.minor,
                                "table-ref",
                                f"{rel} row {r + 1}: references missing ch-{ref:02d}",
                            )
                        )

    drafts_dir = project.root / ".stoner" / "drafts"
    if drafts_dir.exists():
        store = DraftStore(project)
        for d in sorted(drafts_dir.iterdir()):
            m = re.fullmatch(r"ch-(\d{2,})", d.name)
            if not m:
                continue
            n = int(m.group(1))
            if n not in have:
                findings.append(
                    _finding(
                        Severity.minor,
                        "orphan-drafts",
                        f".stoner/drafts/{d.name} has no manuscript chapter",
                    )
                )
                continue
            manifest = store.load_manifest(n)
            if manifest.last_result_sha256:
                _fm, body = project.read_chapter(n)
                if body_hash(body) != manifest.last_result_sha256:
                    findings.append(
                        _finding(
                            Severity.info,
                            "hand-edit",
                            f"ch-{n:02d} was edited since the last harness write "
                            "(will be preserved as a human-edit snapshot)",
                        )
                    )

    return findings


def has_gating_findings(findings: list[Finding]) -> bool:
    """True when any integrity finding is severe enough to fail a refactor."""
    return any(
        f.source == INTEGRITY_SOURCE and f.severity in (Severity.major, Severity.critical)
        for f in findings
    )
