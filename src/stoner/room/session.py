"""run_room_session: the end-to-end Writers' Room session engine.

Flow (chapter scope): reconcile human dismissals into notebooks -> re-locate
each editor's open flags (deterministic tiers, at most one batched LLM
fallback) -> one take per editor per assigned pass (the existing
`ReviewPass.build_prompt` output wrapped with persona + notebook digest +
prior-flag recap; existing parsers reused verbatim) -> one cross-examination
call per editor (agreements/disagreements/priority-rank, comment responses,
notebook opinion refresh riding along) -> comment-obligation settlement (one
follow-up call at most; still-unanswered comments flagged unmet, never
dropped) -> notebook updates -> persisted session record (JSON + Markdown)
under `.stoner/room/sessions/` -> `room.session` ledger entry.

Book scope (`--book`) assembles the manuscript with `review/book_review.py`'s
caps (all chapters verbatim up to 12; beyond that the last 6 full plus rolling
memory summaries -- constants mirrored, not imported, per invariant 4) and
substitutes the assembled text for the chapter body in a synthetic
`PassContext` (chapter=0, prior_tail empty). Book-scope re-location runs the
deterministic tiers per chapter and skips the LLM fallback (drifters stay
open as unlocatable), keeping the whole session within the R17 call bound.

Cost bound (R17): one call per editor per assigned pass + one cross-exam per
editor + at most one re-locate fallback + at most one comment follow-up.
Every call is a plain single-shot completion (no tool loop) so text-only
providers work unchanged (R18). Any single call failure degrades to an info
finding (source `room:<slug>`) and the session continues (R7). Nothing here
gates: sessions are advisory by construction.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..canon.memory import Memory
from ..canon.store import CanonStore
from ..config import StonerConfig
from ..ledger import Ledger
from ..pipelines.common import render_prompt
from ..project import WritingProject
from ..providers.base import Provider, ProviderError
from ..providers.registry import get_provider
from ..review.passes import PASSES, PassContext, build_context, extract_json, locate_span
from ..types import CompletionRequest, Finding, Message, Severity, Usage
from .comments import Comment, CommentStore
from .notebook import Notebook
from .relocate import RelocationOutcome, relocate_items
from .roster import Editor, load_roster

# Mirror book_review's assembly caps (see its module docstring); deliberately
# not imported -- that module's `_assemble_manuscript` is private surface.
_FULL_TEXT_LIMIT = 12
_RECENT_FULL = 6

_SESSIONS_DIR = ".stoner/room/sessions"


@dataclass
class RoomSessionResult:
    """What a session produced. Never prints -- the CLI renders this."""

    session_id: str = ""
    scope: str = "chapter"  # chapter | book
    chapter: int | None = None
    editors: list[str] = field(default_factory=list)
    findings_count: int = 0
    findings_by_editor: dict[str, int] = field(default_factory=dict)
    agreements: int = 0
    disagreements: int = 0
    relocated: dict[str, int] = field(default_factory=dict)  # outcome -> count
    obligations_unmet: list[str] = field(default_factory=list)  # comment ids
    usage: Usage = field(default_factory=Usage)
    notes: list[str] = field(default_factory=list)
    json_path: str = ""
    md_path: str = ""


# ---------------------------------------------------------------------------
# Provider / call helpers
# ---------------------------------------------------------------------------


def _resolve_provider(
    model: str | None, config: StonerConfig, provider: Provider | None
) -> tuple[Provider, str, str]:
    """(provider, bare model id, full model string). Room model resolution:
    explicit override > room.model > the reviewer role."""
    model_str = model or config.room.model or config.models.reviewer
    if provider is not None:
        model_id = model_str.split("/", 1)[1] if "/" in model_str else model_str
        return provider, model_id, model_str
    prov, model_id = get_provider(model_str, config)
    return prov, model_id, model_str


def _complete(
    provider: Provider, model_id: str, project: WritingProject, system: str, user: str
) -> tuple[str, Usage]:
    """One plain single-shot completion (no tools -- R18). Raises ProviderError."""
    resp = provider.complete(
        CompletionRequest(
            model=model_id,
            system=system,
            messages=[Message(role="user", content=user)],
            max_tokens=project.config.max_tokens,
            temperature=project.config.temperature,
        )
    )
    return resp.text, resp.usage


# ---------------------------------------------------------------------------
# Book assembly (mirrors book_review's caps)
# ---------------------------------------------------------------------------


def _assemble_book(project: WritingProject) -> tuple[str, dict[int, str], list[int], list[int]]:
    """(assembled text, {n: body}, full-text nums, summarized nums)."""
    chapters = project.chapters()
    numbers = [c.number for c in chapters]
    bodies: dict[int, str] = {}
    for c in chapters:
        try:
            _, bodies[c.number] = project.read_chapter(c.number)
        except Exception:  # noqa: BLE001 - a broken chapter file is skipped, not fatal
            bodies[c.number] = ""

    memory = Memory(project)
    full: list[int]
    summarized: list[int] = []
    if len(numbers) <= _FULL_TEXT_LIMIT:
        full = list(numbers)
    else:
        recent = set(numbers[-_RECENT_FULL:])
        full = [n for n in numbers if n in recent]
        summarized = [n for n in numbers if n not in recent]

    full_set = set(full)
    parts: list[str] = []
    for n in numbers:
        if n in full_set:
            parts.append(f"## Chapter {n} (full text)\n\n{bodies[n].strip()}")
        else:
            summary = memory.get_chapter_summary(n) or "(no summary recorded)"
            parts.append(f"## Chapter {n} (summary only)\n\n{summary.strip()}")
    return "\n\n".join(parts), bodies, full, summarized


def _book_context(project: WritingProject, assembled: str) -> PassContext:
    """Synthetic whole-book PassContext (chapter=0, prior_tail empty)."""
    store = CanonStore(project)
    return PassContext(
        project=project,
        chapter=0,
        chapter_body=assembled,
        canon_digest=store.context_pack(max_chars=8000),
        memory_context=Memory(project).book_so_far,
        style_excerpt=store.style_body_without_banned(),
        prior_tail="",
    )


# ---------------------------------------------------------------------------
# Prompt wrapping and cross-exam parsing
# ---------------------------------------------------------------------------


def _wrap_take(
    editor: Editor, system: str, user: str, digest: str, recap: str
) -> tuple[str, str]:
    """Editor wrapping at the (system, user) tuple level: persona prepended,
    notebook digest + prior-flag recap appended. Parsers untouched."""
    wrapped_system = f"{editor.persona}\n\n{system}" if editor.persona else system
    parts = [user]
    if digest:
        parts.append(f"## Your Notebook\n\n{digest}")
    if recap:
        parts.append(f"## Your Prior Flags On This Chapter\n\n{recap}")
    return wrapped_system, "\n\n".join(parts)


def _prior_flag_recap(notebook: Notebook, chapter: int | None) -> str:
    lines: list[str] = []
    for it in notebook.tracked_items(chapter):
        esc = int(it.get("escalations", 0))
        if esc <= 0:
            continue
        first = it.get("first_session", "an earlier session")
        lines.append(
            f"- You flagged this in session {first} and it persists "
            f"(seen {esc + 1} time(s)): {it.get('issue', '')}"
        )
    return "\n".join(lines)


def _parse_crossexam(text: str) -> dict[str, Any]:
    """Tolerant, field-by-field parse of the cross-exam JSON (never raises)."""
    data = extract_json(text)
    out: dict[str, Any] = {
        "agreements": [],
        "disagreements": [],
        "priority_rank": [],
        "comment_responses": [],
        "notebook_note": "",
    }
    for key in ("agreements", "disagreements"):
        raw = data.get(key)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("finding_id"):
                    out[key].append(
                        {"finding_id": str(item["finding_id"]), "note": str(item.get("note", ""))}
                    )
    rank = data.get("priority_rank")
    if isinstance(rank, list):
        out["priority_rank"] = [str(r) for r in rank if isinstance(r, (str, int))]
    responses = data.get("comment_responses")
    if isinstance(responses, list):
        for item in responses:
            if isinstance(item, dict) and item.get("comment_id"):
                out["comment_responses"].append(
                    {
                        "comment_id": str(item["comment_id"]),
                        "response": str(item.get("response", "")),
                    }
                )
    note = data.get("notebook_note")
    if isinstance(note, str):
        out["notebook_note"] = note.strip()
    return out


def _findings_block(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        quote = f" — “{f.quote}”" if f.quote else ""
        lines.append(f"- [{f.id}] ({f.severity.value}/{f.category}) {f.issue}{quote}")
    return "\n".join(lines) or "(none)"


def _comments_block(comments: list[Comment]) -> str:
    lines = []
    for c in comments:
        quote = f" — pinned to: “{c.quote}”" if c.quote else ""
        lines.append(f"- [{c.id}] {c.text}{quote}")
    return "\n".join(lines) or "(none)"


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_markdown(record: dict[str, Any], persisting_items: list[dict[str, Any]]) -> str:
    order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
    lines = [
        f"# Writers' Room session: {record['id']}",
        "",
        f"**Scope:** {record['scope']}"
        + (f" (chapter {record['chapter']})" if record.get("chapter") else ""),
        f"**Editors:** {', '.join(record['editors'])}  ",
        f"**Model:** {record['model']}",
        "",
    ]

    if persisting_items:
        lines += ["## On the record", ""]
        for it in persisting_items:
            esc = int(it.get("escalations", 0))
            lines.append(
                f"- (ch {it.get('chapter')}) flagged in session {it.get('first_session')}, "
                f"persists x{esc}: {it.get('issue', '')}"
            )
        lines.append("")

    by_editor: dict[str, list[dict[str, Any]]] = {}
    for f in record.get("findings", []):
        source = str(f.get("source", ""))
        slug = source.split(":")[1] if source.count(":") >= 1 else source
        by_editor.setdefault(slug, []).append(f)
    lines += [f"## Findings ({len(record.get('findings', []))})", ""]
    for slug in record["editors"]:
        editor_findings = by_editor.get(slug, [])
        if not editor_findings:
            continue
        lines.append(f"### {slug}")
        lines.append("")
        for f in sorted(editor_findings, key=lambda f: order.get(str(f.get("severity")), 4)):
            quote = f" — `{f['quote']}`" if f.get("quote") else ""
            lines.append(f"- **[{f.get('severity')}]** ({f.get('category', '')}) {f.get('issue', '')}{quote}")
        lines.append("")

    lines += ["## Cross-examination transcript", ""]
    any_position = False
    for xe in record.get("cross_exam", []):
        editor = xe.get("editor", "")
        for a in xe.get("agreements", []):
            lines.append(f"- **{editor}** agrees with `{a['finding_id']}`: {a['note']}")
            any_position = True
        for d in xe.get("disagreements", []):
            lines.append(f"- **{editor}** disagrees with `{d['finding_id']}`: {d['note']}")
            any_position = True
    if not any_position:
        lines.append("_No agreements or disagreements filed._")
    lines.append("")

    unmet = record.get("obligations_unmet", [])
    if unmet:
        lines += [
            "## Unmet obligations",
            "",
            *[f"- comment `{cid}` is still unanswered" for cid in unmet],
            "",
        ]

    usage = record.get("usage", {})
    lines += [
        "## Usage",
        "",
        f"- input_tokens: {usage.get('input_tokens', 0)}",
        f"- output_tokens: {usage.get('output_tokens', 0)}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session engine
# ---------------------------------------------------------------------------


def load_session_records(project: WritingProject) -> list[dict[str, Any]]:
    """All saved session records (unordered); malformed files are skipped."""
    base = project.root / ".stoner" / "room" / "sessions"
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(base.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def run_room_session(
    project: WritingProject,
    chapter: int | None = None,
    book: bool = False,
    model: str | None = None,
    provider: Provider | None = None,
) -> RoomSessionResult:
    """Run one Writers' Room session over a chapter or the whole book.

    Advisory by construction: never raises for individual call failures (they
    degrade to info findings), and produces no exit-code signal -- the CLI
    always exits 0 on a completed session.
    """
    if not book and chapter is None:
        raise ValueError("a chapter number is required unless --book is given")
    if book and not project.chapters():
        raise ValueError("No chapters to review — draft some chapters first (stoner book).")

    config = project.config
    prov, model_id, model_str = _resolve_provider(model, config, provider)
    roster = load_roster(config.room)
    ledger = Ledger(project.root)
    comment_store = CommentStore(project)
    usage = Usage()
    notes: list[str] = []

    ts = int(time.time())
    scope = "book" if book else "chapter"
    session_id = f"book-{ts}" if book else f"ch-{chapter:02d}-{ts}"

    for editor in roster:
        for unknown in editor.unknown_passes:
            notes.append(f"editor {editor.slug!r}: unknown pass {unknown!r} skipped")

    # -- 0. reconcile human dismissals into notebooks (human authority) ------
    prior_records = load_session_records(project)
    notebooks = {e.slug: Notebook(project, e.slug, config.room) for e in roster}
    for nb in notebooks.values():
        nb.reconcile_dismissals(prior_records)

    # -- context / bodies ----------------------------------------------------
    if book:
        assembled, bodies, chapters_full, chapters_summarized = _assemble_book(project)
        ctx = _book_context(project, assembled)
    else:
        assert chapter is not None
        ctx = build_context(project, chapter)
        bodies = {chapter: ctx.chapter_body}
        chapters_full, chapters_summarized = [chapter], []

    # -- 1. re-locate prior open flags ---------------------------------------
    # All editors' items are batched together so the LLM fallback is at most
    # ONE call for the whole session (R9/R17), not one per editor.
    relocation_records: list[dict[str, Any]] = []
    relocated_counts: dict[str, int] = {}
    all_items: list[dict[str, Any]] = []
    item_editor: dict[str, str] = {}
    for editor in roster:
        for it in notebooks[editor.slug].tracked_items(None if book else chapter):
            all_items.append(it)
            item_editor[str(it.get("id", ""))] = editor.slug
    outcomes: list[RelocationOutcome] = []
    if all_items:
        if book:
            # Deterministic tiers per chapter; no LLM fallback in book scope
            # (keeps the session inside the R17 bound -- see module docstring).
            by_chapter: dict[int, list[dict[str, Any]]] = {}
            for it in all_items:
                by_chapter.setdefault(int(it.get("chapter", 0)), []).append(it)
            for n, chapter_items in by_chapter.items():
                got, _u = relocate_items(
                    bodies.get(n, ""), chapter_items, project, provider=None, llm_relocate=False
                )
                outcomes.extend(got)
        else:
            outcomes, reloc_usage = relocate_items(
                ctx.chapter_body, all_items, project, provider=prov, model_id=model_id
            )
            usage = usage + reloc_usage
        for oc in outcomes:
            slug = item_editor.get(oc.item_id, "")
            owner = notebooks.get(slug)
            relocated_counts[oc.outcome] = relocated_counts.get(oc.outcome, 0) + 1
            relocation_records.append(
                {
                    "editor": slug,
                    "item_id": oc.item_id,
                    "outcome": oc.outcome,
                    "tier": oc.tier,
                    **({"new_quote": oc.new_quote} if oc.new_quote else {}),
                }
            )
            if owner is None:
                continue
            if oc.outcome == "persisting":
                owner.mark_persisting(oc.item_id, session_id, new_quote=oc.new_quote or None)
            elif oc.outcome == "resolved":
                owner.mark_status(oc.item_id, "resolved", session_id)
            else:
                owner.mark_status(oc.item_id, "unlocatable", session_id)
        ledger.append(
            "room.relocate",
            target=_SESSIONS_DIR,
            session=session_id,
            outcomes={oc.item_id: oc.outcome for oc in outcomes},
        )

    # -- 2. takes: one call per editor per assigned pass ----------------------
    all_findings: list[Finding] = []
    findings_by_editor: dict[str, list[Finding]] = {e.slug: [] for e in roster}
    takes: dict[str, list[str]] = {e.slug: [] for e in roster}
    for editor in roster:
        nb = notebooks[editor.slug]
        digest = nb.digest(None if book else chapter)
        recap = _prior_flag_recap(nb, None if book else chapter)
        for pass_name in editor.passes:
            pass_obj = PASSES[pass_name]
            source = f"room:{editor.slug}:{pass_name}"
            try:
                system, user = pass_obj.build_prompt(ctx)
                system, user = _wrap_take(editor, system, user, digest, recap)
                text, call_usage = _complete(prov, model_id, project, system, user)
                usage = usage + call_usage
                findings = pass_obj.parse(text)
                for f in findings:
                    f.source = source
                    if f.quote and f.span is None:
                        f.span = locate_span(ctx.chapter_body, f.quote)
                summary = str(extract_json(text).get("summary", "")).strip()
                if summary:
                    takes[editor.slug].append(f"{pass_name}: {summary}")
            except ProviderError as e:
                findings = [
                    Finding(
                        source=f"room:{editor.slug}",
                        severity=Severity.info,
                        issue=f"take failed ({pass_name}): {e}",
                    )
                ]
                notes.append(f"take failed for {editor.slug}/{pass_name}: {e}")
            except Exception as e:  # noqa: BLE001 - any take failure degrades, never aborts (R7)
                findings = [
                    Finding(
                        source=f"room:{editor.slug}",
                        severity=Severity.info,
                        issue=f"take failed ({pass_name}): {e}",
                    )
                ]
                notes.append(f"take failed for {editor.slug}/{pass_name}: {e}")
            findings_by_editor[editor.slug].extend(findings)
            all_findings.extend(findings)

    # -- 3. cross-examination: one call per editor -----------------------------
    open_comments: list[Comment] = []
    if book:
        for info in project.chapters():
            open_comments.extend(comment_store.list(info.number, only_open=True))
    else:
        assert chapter is not None
        open_comments = comment_store.list(chapter, only_open=True)

    cross_exam: list[dict[str, Any]] = []
    answered_ids: set[str] = set()
    for editor in roster:
        others = [f for f in all_findings if not f.source.startswith(f"room:{editor.slug}")]
        own = findings_by_editor[editor.slug]
        system = render_prompt(
            "room_crossexam.md",
            {
                "project_name": config.project_name,
                "editor_name": editor.name,
                "persona": editor.persona,
            },
        )
        recap = _prior_flag_recap(notebooks[editor.slug], None if book else chapter)
        user_parts = [
            f"## Your findings this session\n\n{_findings_block(own)}",
            f"## The other editors' findings\n\n{_findings_block(others)}",
            f"## Open margin comments from the writer\n\n{_comments_block(open_comments)}",
        ]
        if recap:
            user_parts.append(f"## Your flags on the record\n\n{recap}")
        entry: dict[str, Any] = {
            "editor": editor.slug,
            "agreements": [],
            "disagreements": [],
            "priority_rank": [],
            "comment_responses": [],
            "notebook_note": "",
        }
        try:
            text, call_usage = _complete(prov, model_id, project, system, "\n\n".join(user_parts))
            usage = usage + call_usage
            entry.update(_parse_crossexam(text))
        except ProviderError as e:
            all_findings.append(
                Finding(
                    source=f"room:{editor.slug}",
                    severity=Severity.info,
                    issue=f"cross-examination failed: {e}",
                )
            )
            notes.append(f"cross-examination failed for {editor.slug}: {e}")
        except Exception as e:  # noqa: BLE001 - degrades, never aborts (R7)
            all_findings.append(
                Finding(
                    source=f"room:{editor.slug}",
                    severity=Severity.info,
                    issue=f"cross-examination failed: {e}",
                )
            )
            notes.append(f"cross-examination failed for {editor.slug}: {e}")
        cross_exam.append(entry)

        # Comment responses from this editor settle obligations immediately.
        comment_by_id = {c.id: c for c in open_comments}
        for cr in entry["comment_responses"]:
            c = comment_by_id.get(cr["comment_id"])
            if c is None or not cr.get("response"):
                continue
            comment_store.append_response(
                c.chapter, c.id, editor=editor.slug, text=cr["response"], session=session_id
            )
            answered_ids.add(c.id)

    # -- 4. comment-obligation backstop: at most ONE follow-up call ------------
    unanswered = [c for c in open_comments if c.id not in answered_ids]
    obligations_unmet: list[str] = []
    if unanswered:
        system = render_prompt("room_comment_followup.md", {"project_name": config.project_name})
        user = (
            f"## Chapter text\n\n{ctx.chapter_body}\n\n"
            f"## Unanswered margin comments\n\n{_comments_block(unanswered)}"
        )
        followup_answered: set[str] = set()
        try:
            text, call_usage = _complete(prov, model_id, project, system, user)
            usage = usage + call_usage
            data = extract_json(text)
            raw = data.get("comment_responses")
            comment_by_id = {c.id: c for c in unanswered}
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    c = comment_by_id.get(str(item.get("comment_id", "")))
                    response = str(item.get("response", ""))
                    if c is None or not response:
                        continue
                    comment_store.append_response(
                        c.chapter, c.id, editor="room", text=response, session=session_id
                    )
                    followup_answered.add(c.id)
        except ProviderError as e:
            notes.append(f"comment follow-up failed: {e}")
        except Exception as e:  # noqa: BLE001 - degrades, never aborts (R7)
            notes.append(f"comment follow-up failed: {e}")
        obligations_unmet = [c.id for c in unanswered if c.id not in followup_answered]
        if obligations_unmet:
            notes.append(
                f"{len(obligations_unmet)} comment obligation(s) unmet: "
                + ", ".join(obligations_unmet)
            )

    # -- 5. notebook updates ---------------------------------------------------
    xe_by_editor = {xe["editor"]: xe for xe in cross_exam}
    for editor in roster:
        nb = notebooks[editor.slug]
        for f in findings_by_editor[editor.slug]:
            if f.severity is Severity.info:
                continue  # info findings (degradations, distributions) are not tracked
            f_chapter = chapter if not book else 0
            nb.upsert_item(
                f.id,
                chapter=f_chapter or 0,
                quote=f.quote,
                issue=f.issue,
                severity=f.severity.value,
                pass_name=f.source.rsplit(":", 1)[-1],
                session_id=session_id,
            )
        note = xe_by_editor.get(editor.slug, {}).get("notebook_note", "")
        if note:
            nb.set_opinion(note)
        nb.evict()
        ledger.append("room.notebook.update", target=nb.rel, session=session_id, editor=editor.slug)

    # -- 6. persist the session record ------------------------------------------
    record: dict[str, Any] = {
        "id": session_id,
        "scope": scope,
        "chapter": chapter,
        "editors": [e.slug for e in roster],
        "relocation": relocation_records,
        "findings": [f.model_dump(mode="json") for f in all_findings],
        "takes": {slug: " / ".join(parts) for slug, parts in takes.items() if parts},
        "cross_exam": cross_exam,
        "obligations_unmet": obligations_unmet,
        "chapters_full": chapters_full,
        "chapters_summarized": chapters_summarized,
        "model": model_str,
        "usage": usage.model_dump(),
        "created_at": time.time(),
        "notes": notes,
    }
    persisting = [
        it
        for nb in notebooks.values()
        for it in nb.items()
        if it.get("status") == "persisting"
    ]
    json_path = project.write(f"{_SESSIONS_DIR}/{session_id}.json", json.dumps(record, indent=2, ensure_ascii=False))
    md_path = project.write(f"{_SESSIONS_DIR}/{session_id}.md", _render_markdown(record, persisting))

    ledger.append(
        "room.session",
        target=f"{_SESSIONS_DIR}/{session_id}.json",
        session=session_id,
        scope=scope,
        chapter=chapter,
        editors=[e.slug for e in roster],
        findings=len(all_findings),
        usage=usage.model_dump(),
    )

    agreements = sum(len(xe["agreements"]) for xe in cross_exam)
    disagreements = sum(len(xe["disagreements"]) for xe in cross_exam)
    return RoomSessionResult(
        session_id=session_id,
        scope=scope,
        chapter=chapter,
        editors=[e.slug for e in roster],
        findings_count=len(all_findings),
        findings_by_editor={slug: len(fs) for slug, fs in findings_by_editor.items()},
        agreements=agreements,
        disagreements=disagreements,
        relocated=relocated_counts,
        obligations_unmet=obligations_unmet,
        usage=usage,
        notes=notes,
        json_path=str(json_path),
        md_path=str(md_path),
    )
