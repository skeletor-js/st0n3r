"""Table-read scripts: deterministic-first dialogue attribution + voice map.

Quote extraction handles straight `"..."` and curly `"..."` pairs (the corpus
uses straight). Each chapter becomes an ordered list of narration/dialogue
segments; every dialogue segment records how its speaker was resolved, walking
this ladder (each rung only for what the previous left open):

1. tag -- a known character name sitting directly beside a speech verb in the
   same paragraph (`Ruth said`, `said Denny`);
2. sole -- exactly one known character named in the paragraph;
3. alternation -- inside one scene, once two turns are tagged to two speakers,
   untagged turns alternate; a third tagged speaker resets the pair;
4. UNKNOWN -- honestly unresolved, voiced by the narrator (never guessed).

`--assist` (see `assist_unknowns`) batches only the UNKNOWN lines to the cheap
archivist role for advisory `attribution: llm` labels; a bad reply leaves them
UNKNOWN. Scripts and the voice map are human-editable files: regeneration
refuses to clobber `attribution: manual` edits without `--force`, and the
voice map preserves existing assignments.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..canon.store import CanonStore
from ..ledger import Ledger
from ..project import WritingProject
from ..providers.base import Provider
from .assemble import SceneBreak, parse_prose
from .manifest import ShipError

NARRATOR = "narrator"
UNKNOWN = "UNKNOWN"

#: Verbs that mark a speech tag. Kept tight so "Denny would have said it"
#: (name not adjacent to the verb) does not falsely tag as Denny.
_SPEECH_VERBS = (
    "said", "asked", "answered", "replied", "called", "whispered", "muttered",
    "shouted", "added", "told", "murmured", "continued", "cried", "snapped",
)
_VERB_RE = "|".join(_SPEECH_VERBS)

# Attribution methods that count as a human-owned edit (clobber-guarded).
_MANUAL = "manual"


@dataclass
class Segment:
    kind: str  # narration | dialogue
    speaker: str  # slug | narrator | UNKNOWN
    text: str
    attribution: str = ""  # tag | sole | alternation | llm | manual (dialogue only)


@dataclass
class Script:
    chapter: int
    segments: list[Segment] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {"chapter": self.chapter, "segments": [asdict(s) for s in self.segments]},
            indent=2,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> Script:
        data = json.loads(raw)
        segs = [Segment(**s) for s in data.get("segments", [])]
        return cls(chapter=int(data.get("chapter", 0)), segments=segs)

    def has_manual_edits(self) -> bool:
        return any(s.attribution == _MANUAL for s in self.segments)


# ---------------------------------------------------------------------------
# Character inventory
# ---------------------------------------------------------------------------


@dataclass
class Cast:
    """Name-token -> slug resolution, keyed by unambiguous tokens only."""

    slugs: list[str]
    token_to_slug: dict[str, str]  # only tokens that map to a single character

    def names_in(self, text: str) -> set[str]:
        low = text.lower()
        found: set[str] = set()
        for token, slug in self.token_to_slug.items():
            if re.search(rf"\b{re.escape(token)}\b", low):
                found.add(slug)
        return found

    def tagged_in(self, text: str) -> set[str]:
        low = text.lower()
        tagged: set[str] = set()
        for token, slug in self.token_to_slug.items():
            t = re.escape(token)
            if re.search(rf"\b{t}\b\s+(?:{_VERB_RE})\b", low) or re.search(
                rf"\b(?:{_VERB_RE})\s+{t}\b", low
            ):
                tagged.add(slug)
        return tagged


def load_cast(project: WritingProject) -> Cast:
    """Build the cast from `canon/characters/` (templates already excluded)."""
    store = CanonStore(project)
    token_counts: dict[str, set[str]] = {}
    slugs: list[str] = []
    for entry in store.list_entries(kind="character"):
        slug = entry.slug
        slugs.append(slug)
        name = str(entry.frontmatter.get("name") or slug.replace("-", " "))
        tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", name) if len(t) > 1}
        for tok in tokens:
            token_counts.setdefault(tok, set()).add(slug)
    token_to_slug = {tok: next(iter(s)) for tok, s in token_counts.items() if len(s) == 1}
    return Cast(slugs=slugs, token_to_slug=token_to_slug)


# ---------------------------------------------------------------------------
# Quote extraction
# ---------------------------------------------------------------------------

_OPEN_QUOTES = {'"': '"', "“": "”"}


def split_quotes(text: str) -> list[tuple[str, bool]] | None:
    """Split a paragraph into (text, is_dialogue) runs.

    Straight quotes use the same char to open and close; curly quotes pair
    `"`/`"`. An unclosed quote returns None -> the caller degrades the whole
    paragraph to narration rather than crashing.
    """
    segs: list[tuple[str, bool]] = []
    buf: list[str] = []
    close: str | None = None

    def flush(is_dialogue: bool) -> None:
        if buf:
            segs.append(("".join(buf), is_dialogue))
            buf.clear()

    for ch in text:
        if close is None:
            if ch in _OPEN_QUOTES:
                flush(False)
                close = _OPEN_QUOTES[ch]
                continue
            buf.append(ch)
        else:
            if ch == close:
                flush(True)
                close = None
                continue
            buf.append(ch)
    if close is not None:
        return None  # unclosed
    flush(False)
    return segs


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


class _Alternation:
    """Two-speaker alternation state, reset at each scene break."""

    def __init__(self) -> None:
        self.pair: list[str] = []  # last two distinct tag-established speakers
        self.last: str | None = None  # last resolved dialogue speaker

    def reset(self) -> None:
        self.pair = []
        self.last = None

    def note_tag(self, slug: str) -> None:
        if slug in self.pair:
            self.pair.remove(slug)
        self.pair.append(slug)
        self.pair = self.pair[-2:]

    def alternate(self) -> str | None:
        if len(self.pair) == 2 and self.last in self.pair:
            return self.pair[0] if self.pair[1] == self.last else self.pair[1]
        return None


def _attribute(text: str, cast: Cast, state: _Alternation) -> tuple[str, str]:
    """Resolve a dialogue paragraph's speaker. Returns (speaker, method)."""
    tagged = cast.tagged_in(text)
    if len(tagged) == 1:
        slug = next(iter(tagged))
        state.note_tag(slug)
        state.last = slug
        return slug, "tag"

    named = cast.names_in(text)
    if len(named) == 1:
        slug = next(iter(named))
        state.last = slug
        return slug, "sole"

    alt = state.alternate()
    if alt is not None:
        state.last = alt
        return alt, "alternation"

    return UNKNOWN, ""


def build_script(project: WritingProject, chapter: int, cast: Cast | None = None) -> Script:
    """Extract and attribute one chapter's table-read script (deterministic)."""
    cast = cast or load_cast(project)
    _fm, body = project.read_chapter(chapter)
    script = Script(chapter=chapter)
    state = _Alternation()

    for block in parse_prose(body):
        if isinstance(block, SceneBreak):
            state.reset()
            continue
        # Paragraphs and Headings both carry `.text`; narration for headings.
        text = getattr(block, "text", "")
        if not text:
            continue
        runs = split_quotes(text)
        if runs is None:
            # Unclosed quote: degrade the whole paragraph to narration.
            script.segments.append(Segment("narration", NARRATOR, text.strip()))
            continue
        has_dialogue = any(is_d for _t, is_d in runs)
        speaker, method = (UNKNOWN, "")
        if has_dialogue:
            speaker, method = _attribute(text, cast, state)
        for run_text, is_dialogue in runs:
            stripped = run_text.strip()
            if not stripped:
                continue
            if is_dialogue:
                script.segments.append(Segment("dialogue", speaker, stripped, method))
            else:
                script.segments.append(Segment("narration", NARRATOR, stripped))
    return script


# ---------------------------------------------------------------------------
# LLM assist (advisory)
# ---------------------------------------------------------------------------


def assist_unknowns(
    project: WritingProject,
    script: Script,
    cast: Cast | None = None,
    model: str | None = None,
    provider: Provider | None = None,
) -> tuple[Script, object]:
    """Resolve UNKNOWN dialogue lines via the archivist role (advisory).

    STRICT-JSON reply parsed tolerantly (mirrors review.passes.extract_json);
    any line the model does not confidently map stays UNKNOWN (R13). Returns
    (script, usage)."""
    from ..pipelines.common import call_model, render_prompt
    from ..review.passes import extract_json
    from ..types import Usage

    cast = cast or load_cast(project)
    unknown_idx = [i for i, s in enumerate(script.segments) if s.kind == "dialogue" and s.speaker == UNKNOWN]
    if not unknown_idx:
        return script, Usage()

    lines = []
    for n, idx in enumerate(unknown_idx):
        # A one-segment context window: the line plus its neighbors.
        prev = script.segments[idx - 1].text if idx > 0 else ""
        nxt = script.segments[idx + 1].text if idx + 1 < len(script.segments) else ""
        lines.append(
            f'{n}: (before: {prev[:80]!r}) LINE: "{script.segments[idx].text}" (after: {nxt[:80]!r})'
        )
    context = {
        "characters": "\n".join(f"- {slug}" for slug in cast.slugs),
        "lines": "\n".join(lines),
    }
    system = (
        "You are a dialogue-attribution assistant. You map ambiguous quoted "
        "lines to the character slug most likely speaking them. You never guess "
        "wildly: if a line is genuinely unclear, return \"UNKNOWN\"."
    )
    user = render_prompt("ship_attribution.md", context)
    text, usage = call_model(project, "archivist", system, user, model=model, provider=provider)
    data = extract_json(text)
    mapping = data.get("attributions") if isinstance(data, dict) else None
    valid = set(cast.slugs)
    if isinstance(mapping, dict):
        for key, value in mapping.items():
            try:
                n = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= n < len(unknown_idx) and isinstance(value, str) and value in valid:
                seg = script.segments[unknown_idx[n]]
                seg.speaker = value
                seg.attribution = "llm"
    return script, usage


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def script_path(project: WritingProject, chapter: int) -> Path:
    return project.resolve(f"export/audio/scripts/ch-{chapter:02d}.json")


def load_script(project: WritingProject, chapter: int) -> Script | None:
    path = script_path(project, chapter)
    if not path.exists():
        return None
    return Script.from_json(path.read_text(encoding="utf-8"))


def write_script(project: WritingProject, script: Script, force: bool = False) -> Path:
    """Write a chapter script, refusing to clobber manual edits without force."""
    existing = load_script(project, script.chapter)
    if existing is not None and existing.has_manual_edits() and not force:
        raise ShipError(
            f"ch-{script.chapter:02d} script carries manual attribution edits; "
            "pass --force to overwrite them."
        )
    path = script_path(project, script.chapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script.to_json(), encoding="utf-8")
    Ledger(project.root).append(
        "ship.script", target=str(path.relative_to(project.root)), chapter=script.chapter
    )
    return path


# ---------------------------------------------------------------------------
# Voice map
# ---------------------------------------------------------------------------


def count_dialogue_lines(project: WritingProject, chapters: list[int]) -> dict[str, int]:
    """Dialogue-line counts per character slug across the given chapters."""
    cast = load_cast(project)
    counts: dict[str, int] = {}
    for n in chapters:
        script = build_script(project, n, cast=cast)
        for seg in script.segments:
            if seg.kind == "dialogue" and seg.speaker not in (UNKNOWN, NARRATOR):
                counts[seg.speaker] = counts.get(seg.speaker, 0) + 1
    return counts


def build_voice_map(
    counts: dict[str, int],
    available_voices: list[str],
    narrator_voice: str = "",
    existing: dict | None = None,
) -> dict:
    """Rank characters by line count, round-robin assign backend voices,
    preserve any existing assignments."""
    existing = existing or {}
    existing_chars = dict(existing.get("characters", {}))
    narrator = narrator_voice or existing.get("narrator") or (available_voices[0] if available_voices else "")

    ranked = sorted(counts, key=lambda s: (-counts[s], s))
    assigned: dict[str, str] = {}
    i = 0
    for slug in ranked:
        if slug in existing_chars:
            assigned[slug] = existing_chars[slug]
            continue
        if available_voices:
            assigned[slug] = available_voices[i % len(available_voices)]
            i += 1
        else:
            assigned[slug] = ""
    # Preserve any pre-existing characters not seen in counts.
    for slug, voice in existing_chars.items():
        assigned.setdefault(slug, voice)
    return {"narrator": narrator, "characters": assigned}
