"""Structural refactors: merge, split (deterministic), move-reveal, flip-POV
(model-assisted).

Pipeline functions returning a result dataclass, never printing (the CLI
owns presentation). Snapshot-first discipline everywhere: every affected
body is preserved in `.stoner/drafts/` under its original chapter number
BEFORE any file moves, so no prose is ever unrecoverable -- merge vacates
the highest chapter number with no tombstone stub because the snapshots
plus manifest are the durable record.

Merge and split are pure file mechanics plus the U5 renumber cascade; no
prose is model-touched. Move-reveal and flip-POV are guarded model
pipelines mirroring `review/revise.py`: inline prompt builders, SUMMARY +
`BEGIN CHAPTER`/`END CHAPTER` sentinels, tolerant extraction, and a length
guard that refuses any rewrite under 1/4 of the original words. They
resolve against the `writer` role and use one plain completion per chapter
(no tool loop), so text-only providers work unchanged. All writes go
through the snapshot chokepoint, so a bad rewrite is one `stoner drafts
restore` away.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from ..canon.memory import Memory
from ..canon.store import CanonStore
from ..ledger import Ledger
from ..project import WritingProject, count_words, split_frontmatter
from ..providers.base import Provider
from ..providers.registry import get_provider, parse_model_string
from ..types import CompletionRequest, Finding, Message, Usage
from .renumber import apply_renumber, check_integrity, has_gating_findings, plan_renumber
from .snapshots import DraftStore, body_hash, snapshot_write_chapter

SCENE_BREAK = "* * *"

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")

# Mirrors revise.py's sentinels: tolerant extraction so a model that forgets
# the wrapper but returns plain prose doesn't lose the rewrite.
_BEGIN_RE = re.compile(r"BEGIN\s+CHAPTER\s*\n?", re.IGNORECASE)
_END_RE = re.compile(r"\n?\s*END\s+CHAPTER", re.IGNORECASE)

_CANON_DIGEST_CHARS = 8000


class RefactorError(RuntimeError):
    """Raised when a refactor cannot proceed; the manuscript is untouched."""


@dataclass
class RefactorResult:
    """Outcome of one refactor pipeline run."""

    operation: str
    chapters: list[int]
    mapping: dict[int, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    report_path: str = ""

    @property
    def integrity_failed(self) -> bool:
        return has_gating_findings(self.findings)


def _split_paragraph_blocks(body: str) -> list[str]:
    return [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(body) if p.strip()]


def _snapshot_full_text(
    project: WritingProject,
    number: int,
    full_text: str,
    reason: str,
    detail: dict | None = None,
) -> int:
    """Record `full_text` as a snapshot of chapter `number`; returns the seq."""
    store = DraftStore(project)
    manifest = store.load_manifest(number)
    _fm, body = split_frontmatter(full_text)
    entry = store.record(
        number,
        manifest,
        full_text,
        body_hash(body),
        reason,
        detail=detail,
        result_sha256=body_hash(body),
    )
    store.save_manifest(number, manifest)
    Ledger(project.root).append(
        "drafts.snapshot",
        target=project.chapter_rel(number),
        chapter=number,
        seq=entry.seq,
        reason=reason,
    )
    return entry.seq


def _run_verify(
    result: RefactorResult,
    project: WritingProject,
    affected: list[int],
    verify: bool,
    provider: Provider | None,
    model: str | None,
) -> None:
    """Post-refactor verification wiring (U8): advisory only."""
    if not verify or not project.config.archaeology.verify_after_refactor:
        return
    from .verify import verify_refactor

    vr = verify_refactor(project, affected, provider=provider, model=model)
    # Integrity findings were already collected by the refactor itself;
    # keep only the advisory (model) findings and notes from verification.
    result.findings.extend(f for f in vr.findings if f.source != "drafts:integrity")
    result.notes.extend(vr.notes)
    result.usage += vr.usage
    result.report_path = vr.report_path


# ---------------------------------------------------------------------------
# Deterministic refactors: merge / split
# ---------------------------------------------------------------------------


def merge_chapters(
    project: WritingProject,
    a: int,
    b: int,
    verify: bool = True,
    provider: Provider | None = None,
    model: str | None = None,
) -> RefactorResult:
    """Merge chapter `b` into chapter `a` (must be adjacent, b == a + 1),
    then renumber every chapter above `b` down by one. Deterministic: no
    prose is model-touched. Both original bodies are restorable from
    chapter `a`'s snapshots."""
    if b != a + 1:
        raise RefactorError(f"merge requires adjacent chapters; got {a} and {b}")
    for n in (a, b):
        if not (project.root / project.chapter_rel(n)).exists():
            raise RefactorError(f"chapter {n} not found ({project.chapter_rel(n)})")

    result = RefactorResult(operation="merge", chapters=[a, b])
    fm_a, body_a = project.read_chapter(a)
    text_b = project.read(project.chapter_rel(b))
    _fm_b, body_b = split_frontmatter(text_b)

    # Preserve b's full text in a's manifest BEFORE any file changes, so
    # `stoner drafts restore <a> <seq>` can recover it byte-for-byte.
    seq_b = _snapshot_full_text(
        project, a, text_b, "refactor-merge", detail={"merged_from": b}
    )
    result.notes.append(f"ch-{b:02d} body preserved as ch-{a:02d} snapshot seq {seq_b}")

    merged_body = body_a.rstrip("\n") + f"\n\n{SCENE_BREAK}\n\n" + body_b.lstrip("\n")
    snapshot_write_chapter(
        project, a, fm_a, merged_body, reason="refactor-merge", detail={"merged": [a, b]}
    )

    # Beat sheets: b's content appends to a's under a marker; b's file goes.
    beats_a = project.root / f"outline/beats/ch-{a:02d}.md"
    beats_b = project.root / f"outline/beats/ch-{b:02d}.md"
    if beats_b.exists():
        b_content = beats_b.read_text(encoding="utf-8")
        marker = f"\n\n<!-- merged from ch-{b:02d} -->\n\n"
        if beats_a.exists():
            beats_a.write_text(
                beats_a.read_text(encoding="utf-8").rstrip("\n") + marker + b_content,
                encoding="utf-8",
            )
        else:
            beats_a.write_text(f"<!-- merged from ch-{b:02d} -->\n\n" + b_content, encoding="utf-8")
        beats_b.unlink()
        result.notes.append(f"beats of ch-{b:02d} appended to ch-{a:02d}")

    # Memory: concatenate b's summary under a, drop b's key.
    memory = Memory(project)
    sum_a = memory.get_chapter(a) or {}
    sum_b = memory.get_chapter(b)
    if sum_b:
        combined = " ".join(s for s in (sum_a.get("summary", ""), sum_b.get("summary", "")) if s)
        memory.set_chapter_summary(
            a,
            combined,
            pov=str(sum_a.get("pov", "") or sum_b.get("pov", "")),
            words=count_words(merged_body),
            new_facts=list(sum_a.get("new_facts", [])) + list(sum_b.get("new_facts", [])),
        )
        data = project.read_memory()
        data.get("chapters", {}).pop(str(b), None)
        project.write_memory(data)

    # Book state: fold b away before the renumber cascade.
    state_path = project.root / ".stoner" / "book-state.json"
    if state_path.exists():
        from ..pipelines.book import load_state, save_state

        state = load_state(project)
        state.chapters_planned = [n for n in state.chapters_planned if n != b]
        state.chapters_done.pop(b, None)
        save_state(project, state)

    # Vacate b: the manuscript file goes (body preserved above); b's own
    # drafts history is archived out of the renumber cascade's way.
    (project.root / project.chapter_rel(b)).unlink()
    drafts_b = project.root / ".stoner" / "drafts" / f"ch-{b:02d}"
    if drafts_b.exists():
        archive = drafts_b.parent / f"ch-{b:02d}-merged-{int(time.time())}"
        drafts_b.rename(archive)
        result.notes.append(
            f"draft history of ch-{b:02d} archived at "
            f".stoner/drafts/{archive.name} (plain files, nothing deleted)"
        )

    mapping = {c.number: c.number - 1 for c in project.chapters() if c.number > b}
    result.mapping = mapping
    if mapping:
        plan = plan_renumber(project, mapping)
        apply_renumber(project, plan, reason="refactor-merge")
        result.notes.extend(plan.flags)
    Memory(project).rebuild_book_so_far()

    result.findings.extend(check_integrity(project))
    Ledger(project.root).append(
        "drafts.refactor.merge",
        target=project.chapter_rel(a),
        merged=[a, b],
        mapping={str(k): v for k, v in mapping.items()},
    )
    _run_verify(result, project, [a], verify, provider, model)
    return result


def split_chapter(
    project: WritingProject,
    ch: int,
    at: int | None = None,
    at_text: str | None = None,
    verify: bool = True,
    provider: Provider | None = None,
    model: str | None = None,
) -> RefactorResult:
    """Split chapter `ch` so paragraph `at` (1-based, blank-line blocks)
    starts a new chapter `ch+1`; everything above `ch` renumbers up by one.
    `at_text` locates the split paragraph by exact substring instead."""
    if not (project.root / project.chapter_rel(ch)).exists():
        raise RefactorError(f"chapter {ch} not found ({project.chapter_rel(ch)})")
    fm, body = project.read_chapter(ch)
    blocks = _split_paragraph_blocks(body)
    if at_text is not None:
        matches = [i for i, blk in enumerate(blocks) if at_text in blk]
        if not matches:
            raise RefactorError(f"--at-text {at_text!r} not found in ch-{ch:02d}")
        if len(matches) > 1:
            raise RefactorError(
                f"--at-text {at_text!r} matches {len(matches)} paragraphs in "
                f"ch-{ch:02d}; use a longer, unique marker or --at <n>"
            )
        at = matches[0] + 1
    if at is None:
        raise RefactorError("split needs --at <paragraph> or --at-text <marker>")
    if not 2 <= at <= len(blocks):
        raise RefactorError(
            f"ch-{ch:02d} has {len(blocks)} paragraph(s); --at must be between "
            f"2 and {len(blocks)} so both halves hold prose"
        )

    result = RefactorResult(operation="split", chapters=[ch, ch + 1])
    full_text = project.read(project.chapter_rel(ch))
    _snapshot_full_text(project, ch, full_text, "refactor-split", detail={"at": at})

    mapping = {c.number: c.number + 1 for c in project.chapters() if c.number > ch}
    result.mapping = mapping
    if mapping:
        plan = plan_renumber(project, mapping)
        apply_renumber(project, plan, reason="refactor-split")
        result.notes.extend(plan.flags)

    first_body = "\n\n".join(blocks[: at - 1]) + "\n"
    second_body = "\n\n".join(blocks[at - 1 :]) + "\n"
    snapshot_write_chapter(
        project, ch, fm, first_body, reason="refactor-split", detail={"at": at}
    )
    fm_second = {
        "title": fm.get("title", ""),
        "status": fm.get("status", "draft"),
        "pov": fm.get("pov", ""),
    }
    snapshot_write_chapter(
        project,
        ch + 1,
        fm_second,
        second_body,
        reason="refactor-split",
        detail={"split_from": ch, "at": at},
    )
    result.notes.append(
        f"beat sheet stays with ch-{ch:02d}; ch-{ch + 1:02d} has none yet"
    )
    Memory(project).rebuild_book_so_far()

    result.findings.extend(check_integrity(project))
    Ledger(project.root).append(
        "drafts.refactor.split",
        target=project.chapter_rel(ch),
        split=[ch, ch + 1],
        at=at,
        mapping={str(k): v for k, v in mapping.items()},
    )
    _run_verify(result, project, [ch, ch + 1], verify, provider, model)
    return result


# ---------------------------------------------------------------------------
# Model-assisted refactors: move-reveal / flip-pov
# ---------------------------------------------------------------------------


def _extract_body(text: str) -> str:
    """Tolerant sentinel extraction, mirroring `review.revise`."""
    begin_m = _BEGIN_RE.search(text)
    if begin_m is None:
        return text.strip()
    end_m = _END_RE.search(text, begin_m.end())
    if end_m is not None and end_m.start() > begin_m.end():
        return text[begin_m.end() : end_m.start()].strip("\n")
    return text[begin_m.end() :].strip("\n")


def _resolve_writer(
    project: WritingProject, model: str | None, provider: Provider | None
) -> tuple[Provider, str]:
    from ..pipelines.common import resolve_role_model

    model_str = resolve_role_model(project.config, "writer", model)
    if provider is not None:
        model_id = parse_model_string(model_str)[1] if "/" in model_str else model_str
        return provider, model_id
    return get_provider(model_str, project.config)


def _guarded_completion(
    project: WritingProject,
    prov: Provider,
    model_id: str,
    system: str,
    user: str,
    old_words: int,
    what: str,
) -> tuple[str, Usage]:
    """One plain completion + revise-style length guard. Raises RefactorError
    on a truncated/empty rewrite; nothing has been written at that point."""
    resp = prov.complete(
        CompletionRequest(
            model=model_id,
            system=system,
            messages=[Message(role="user", content=user)],
            max_tokens=project.config.max_tokens,
            temperature=project.config.temperature,
        )
    )
    new_body = _extract_body(resp.text)
    new_words = count_words(new_body)
    if new_words == 0 or new_words < old_words // 4:
        raise RefactorError(
            f"Rewrite for {what} came back with {new_words} words (original: "
            f"{old_words}); refusing to overwrite. The model response was "
            "likely truncated — raise max_tokens in stoner.yaml or retry. "
            "The manuscript is untouched."
        )
    return new_body, resp.usage


def _find_quote(body: str, quote: str) -> bool:
    """`locate_span`-grade tolerant matching: exact first, then
    whitespace/case-normalized."""
    if not quote.strip():
        return False
    if quote in body:
        return True
    norm_body = " ".join(body.split()).lower()
    norm_quote = " ".join(quote.split()).lower()
    return norm_quote in norm_body


_SENTINEL_FORMAT = (
    "Respond with ONLY the following, in exactly this format (no other "
    "prose):\n\n"
    "SUMMARY: <one paragraph describing what you changed>\n"
    "BEGIN CHAPTER\n"
    "<the complete rewritten chapter body>\n"
    "END CHAPTER"
)


def _move_source_prompt(chapter: int, body: str, quote: str, canon_digest: str) -> tuple[str, str]:
    system = (
        "You are a meticulous novelist's assistant restructuring a "
        "manuscript. You remove a reveal from a chapter and heal the seams "
        "so the prose reads as if the reveal was never there. Preserve the "
        "author's voice, plot, and everything unrelated to the reveal."
    )
    parts = [
        f"## Chapter {chapter} (original, full text)\n\n{body}",
    ]
    if canon_digest:
        parts.append(f"## Canon\n\n{canon_digest}")
    parts.append(f"## The reveal to REMOVE\n\n\"{quote}\"")
    parts.append(
        "## Task\n\n"
        "Rewrite the ENTIRE chapter with the reveal removed. Heal the seams: "
        "adjust any sentence that reacts to or depends on the reveal so the "
        "chapter flows naturally without it. Change nothing else.\n\n"
        + _SENTINEL_FORMAT
    )
    return system, "\n\n".join(parts)


def _move_target_prompt(
    chapter: int, body: str, quote: str, position: str, canon_digest: str
) -> tuple[str, str]:
    system = (
        "You are a meticulous novelist's assistant restructuring a "
        "manuscript. You weave a reveal into a chapter so it lands "
        "naturally, preserving the author's voice and everything already "
        "on the page."
    )
    where = "early in the chapter" if position == "early" else "late in the chapter"
    parts = [
        f"## Chapter {chapter} (original, full text)\n\n{body}",
    ]
    if canon_digest:
        parts.append(f"## Canon\n\n{canon_digest}")
    parts.append(f"## The reveal to WEAVE IN ({where})\n\n\"{quote}\"")
    parts.append(
        "## Task\n\n"
        f"Rewrite the ENTIRE chapter with the reveal woven in {where}. "
        "Integrate it into the surrounding prose — do not paste it verbatim "
        "if a smoother phrasing fits the scene. Change nothing unrelated.\n\n"
        + _SENTINEL_FORMAT
    )
    return system, "\n\n".join(parts)


def move_reveal(
    project: WritingProject,
    src: int,
    dst: int,
    quote: str,
    position: str = "early",
    model: str | None = None,
    provider: Provider | None = None,
    verify: bool = True,
) -> RefactorResult:
    """Move a reveal (located by `quote`) from chapter `src` to `dst` via two
    guarded writer-role completions. Nothing is written unless BOTH rewrites
    pass the length guard; both prior bodies are snapshotted with reason
    `refactor-move`."""
    if src == dst:
        raise RefactorError("source and target chapters must differ")
    for n in (src, dst):
        if not (project.root / project.chapter_rel(n)).exists():
            raise RefactorError(f"chapter {n} not found ({project.chapter_rel(n)})")
    fm_src, body_src = project.read_chapter(src)
    fm_dst, body_dst = project.read_chapter(dst)
    if not _find_quote(body_src, quote):
        raise RefactorError(
            f"quote not found in ch-{src:02d}: {quote!r} — copy it verbatim "
            "from the chapter text"
        )

    prov, model_id = _resolve_writer(project, model, provider)
    canon_digest = CanonStore(project).context_pack(max_chars=_CANON_DIGEST_CHARS)
    result = RefactorResult(operation="move-reveal", chapters=[src, dst])

    system, user = _move_source_prompt(src, body_src, quote, canon_digest)
    new_src, usage1 = _guarded_completion(
        project, prov, model_id, system, user, count_words(body_src), f"ch-{src:02d}"
    )
    system, user = _move_target_prompt(dst, body_dst, quote, position, canon_digest)
    new_dst, usage2 = _guarded_completion(
        project, prov, model_id, system, user, count_words(body_dst), f"ch-{dst:02d}"
    )
    result.usage = usage1 + usage2

    # Both rewrites passed the guards: only now touch the manuscript.
    snapshot_write_chapter(
        project, src, fm_src, new_src, reason="refactor-move", detail={"moved_to": dst}
    )
    snapshot_write_chapter(
        project, dst, fm_dst, new_dst, reason="refactor-move", detail={"moved_from": src}
    )

    result.findings.extend(check_integrity(project))
    Ledger(project.root).append(
        "drafts.refactor.move",
        target=project.chapter_rel(src),
        src=src,
        dst=dst,
        position=position,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
    )
    _run_verify(result, project, [src, dst], verify, provider, model)
    return result


def _flip_pov_prompt(
    chapter: int, body: str, to_character: str, canon_digest: str, character_entry: str
) -> tuple[str, str]:
    system = (
        "You are a meticulous novelist's assistant restructuring a "
        "manuscript. You rewrite a chapter from a different point-of-view "
        "character, preserving every plot event, the timeline, and the "
        "author's prose style."
    )
    parts = [f"## Chapter {chapter} (original, full text)\n\n{body}"]
    if canon_digest:
        parts.append(f"## Canon\n\n{canon_digest}")
    if character_entry:
        parts.append(f"## New POV character: {to_character}\n\n{character_entry}")
    parts.append(
        "## Task\n\n"
        f"Rewrite the ENTIRE chapter from {to_character}'s point of view. "
        "Keep every plot event and its order; change only what the new "
        "viewpoint requires (perception, interiority, what this character "
        "can and cannot know).\n\n" + _SENTINEL_FORMAT
    )
    return system, "\n\n".join(parts)


def flip_pov(
    project: WritingProject,
    ch: int,
    to_character: str,
    model: str | None = None,
    provider: Provider | None = None,
    verify: bool = True,
) -> RefactorResult:
    """Rewrite chapter `ch` from `to_character`'s POV via one guarded
    writer-role completion; updates the `pov` frontmatter field."""
    if not (project.root / project.chapter_rel(ch)).exists():
        raise RefactorError(f"chapter {ch} not found ({project.chapter_rel(ch)})")
    fm, body = project.read_chapter(ch)

    prov, model_id = _resolve_writer(project, model, provider)
    store = CanonStore(project)
    canon_digest = store.context_pack(max_chars=_CANON_DIGEST_CHARS)
    entry = store.find_character_by_name(to_character)
    entry_text = entry.body if entry is not None else ""

    result = RefactorResult(operation="flip-pov", chapters=[ch])
    if entry is None:
        result.notes.append(
            f"no canon entry found for {to_character!r} — the rewrite ran on "
            "canon digest alone"
        )

    system, user = _flip_pov_prompt(ch, body, to_character, canon_digest, entry_text)
    new_body, usage = _guarded_completion(
        project, prov, model_id, system, user, count_words(body), f"ch-{ch:02d}"
    )
    result.usage = usage

    new_fm = dict(fm)
    new_fm["pov"] = to_character
    snapshot_write_chapter(
        project, ch, new_fm, new_body, reason="refactor-pov", detail={"pov": to_character}
    )

    result.findings.extend(check_integrity(project))
    Ledger(project.root).append(
        "drafts.refactor.pov",
        target=project.chapter_rel(ch),
        chapter=ch,
        pov=to_character,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
    _run_verify(result, project, [ch], verify, provider, model)
    return result
