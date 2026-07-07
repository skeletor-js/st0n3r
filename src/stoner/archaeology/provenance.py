"""Deterministic sentence-level provenance over the draft snapshot chain.

Pure functions in the `slop/` analyzer style: given a project and chapter,
reconstruct the sequence of body states (snapshots in seq order plus the
current body as head) and attribute every sentence of the current body to
the rewrite event that introduced it. Attribution is exact-match walk-back
(a sentence belongs to the earliest state containing it verbatim, after
whitespace normalization); sentences that appear modified rather than new
are classified with `difflib.SequenceMatcher` and reported as "revised in
<event>, originated in <event>". No model calls anywhere: identical inputs
give identical output.

Producer attribution is derived from the manifest: the content preserved by
entry k was displaced by entry k's event and *produced* by entry k-1's
event (entry 1's content is the initial state); the current body was
produced by the last entry's event. Pruned entries participate via their
stored hashes -- attribution still resolves events on both sides and flags
the missing content.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from pydantic import BaseModel, Field

from ..project import ProjectError, WritingProject, split_frontmatter
from .snapshots import ChapterManifest, DraftStore, SnapshotEntry

# A sentence within this SequenceMatcher ratio of an earlier sentence is a
# revision of it, not new prose.
REVISED_RATIO = 0.6

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+")

INITIAL_EVENT = "initial"


def split_paragraphs(text: str) -> list[str]:
    """Blank-line-separated blocks, stripped, empties dropped."""
    return [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(text) if p.strip()]


def split_sentences(text: str) -> list[str]:
    """Regex sentence tokenizer: terminal punctuation + paragraph boundaries."""
    out: list[str] = []
    for para in split_paragraphs(text):
        for sent in _SENTENCE_SPLIT_RE.split(para):
            s = sent.strip()
            if s:
                out.append(s)
    return out


def normalize_sentence(sentence: str) -> str:
    """Whitespace-normalized comparison key."""
    return " ".join(sentence.split())


class BodyState(BaseModel):
    """One reconstructed body state in the snapshot chain."""

    label: str  # "seq N" or "current"
    body: str
    produced_by: SnapshotEntry | None = None  # None = initial state
    pruned_before: list[int] = Field(default_factory=list)  # seqs missing content


class SentenceProvenance(BaseModel):
    """Attribution of one current-body sentence to the event that wrote it."""

    sentence: str
    event: str  # reason of the producing event, or "initial"
    seq: int | None = None  # manifest seq of the producing entry
    ts: float | None = None
    session: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    git_head: str | None = None
    revised: bool = False  # difflib-classified rewrite of an earlier sentence
    originated_event: str = ""  # where the revised sentence's ancestor first appeared
    originated_seq: int | None = None
    note: str = ""


def _entry_content(store: DraftStore, number: int, entry: SnapshotEntry) -> str | None:
    """Body text preserved by `entry`, or None when pruned/unavailable."""
    if entry.pruned or not entry.file:
        return None
    path = store.chapter_dir(number) / entry.file
    if not path.exists():
        return None
    _fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
    return body


def build_state_chain(project: WritingProject, number: int) -> list[BodyState]:
    """Snapshot bodies in seq order plus the current on-disk body as head.

    Entries whose content is unavailable (pruned, or deduped away with no
    file) are skipped; their seqs are recorded on the next available state's
    `pruned_before` so attribution can flag the gap.
    """
    store = DraftStore(project)
    manifest: ChapterManifest = store.load_manifest(number)
    states: list[BodyState] = []
    missing: list[int] = []
    prev_entry: SnapshotEntry | None = None
    for entry in manifest.entries:
        body = _entry_content(store, number, entry)
        if body is None:
            missing.append(entry.seq)
        else:
            states.append(
                BodyState(
                    label=f"seq {entry.seq}",
                    body=body,
                    produced_by=prev_entry,
                    pruned_before=missing,
                )
            )
            missing = []
        prev_entry = entry
    try:
        _fm, current = project.read_chapter(number)
    except ProjectError:
        current = ""
    states.append(
        BodyState(
            label="current",
            body=current,
            produced_by=manifest.entries[-1] if manifest.entries else None,
            pruned_before=missing,
        )
    )
    return states


def _provenance_from_state(sentence: str, state: BodyState) -> SentenceProvenance:
    entry = state.produced_by
    prov = SentenceProvenance(sentence=sentence, event=INITIAL_EVENT)
    if entry is not None:
        prov.event = entry.reason
        prov.seq = entry.seq
        prov.ts = entry.ts
        prov.session = entry.session
        prov.detail = dict(entry.detail)
        prov.git_head = entry.git_head
    if state.pruned_before:
        pruned = ", ".join(str(s) for s in state.pruned_before)
        prov.note = f"content of seq {pruned} pruned/unavailable"
    return prov


def _earliest_state_with(states: list[BodyState], sentence_keys: list[set[str]], key: str) -> int:
    for i, keys in enumerate(sentence_keys):
        if key in keys:
            return i
    return len(states) - 1


def attribute_sentences(project: WritingProject, number: int) -> list[SentenceProvenance]:
    """Attribute each sentence of the current body to its producing event.

    Deterministic: exact-match walk-back over the state chain, with a
    SequenceMatcher pass classifying lightly reworded sentences as revisions
    of prose from the previous state.
    """
    states = build_state_chain(project, number)
    state_sentences = [split_sentences(s.body) for s in states]
    sentence_keys = [{normalize_sentence(s) for s in sents} for sents in state_sentences]

    out: list[SentenceProvenance] = []
    for sentence in state_sentences[-1]:
        key = normalize_sentence(sentence)
        intro = _earliest_state_with(states, sentence_keys, key)
        prov = _provenance_from_state(sentence, states[intro])
        if intro > 0:
            # Not present in any earlier state: new prose, or a revision of
            # something in the state just before its introduction?
            prev_sents = state_sentences[intro - 1]
            best_ratio = 0.0
            best_sent = ""
            for cand in prev_sents:
                ratio = difflib.SequenceMatcher(None, key, normalize_sentence(cand)).ratio()
                if ratio > best_ratio:
                    best_ratio, best_sent = ratio, cand
            if best_ratio >= REVISED_RATIO and key != normalize_sentence(best_sent):
                origin_idx = _earliest_state_with(
                    states, sentence_keys, normalize_sentence(best_sent)
                )
                origin = _provenance_from_state(best_sent, states[origin_idx])
                prov.revised = True
                prov.originated_event = origin.event
                prov.originated_seq = origin.seq
        out.append(prov)
    return out


def unified_diff(a_text: str, b_text: str, a_label: str, b_label: str) -> str:
    """Plain difflib unified diff between two body texts."""
    return "".join(
        difflib.unified_diff(
            a_text.splitlines(keepends=True),
            b_text.splitlines(keepends=True),
            fromfile=a_label,
            tofile=b_label,
        )
    )
