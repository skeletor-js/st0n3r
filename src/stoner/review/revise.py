"""revise_chapter: apply accepted review findings to a chapter via the model.

Builds one prompt (original chapter + accepted findings + canon digest +
style guidance), asks the model to return the *complete* revised chapter
body between `BEGIN CHAPTER` / `END CHAPTER` sentinels, parses that
tolerantly, and writes the result back through `WritingProject.write_chapter`
so frontmatter is preserved (with `status` bumped to `"revised"`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..canon.store import CanonStore
from ..ledger import Ledger
from ..project import WritingProject, count_words
from ..providers.base import Provider
from ..providers.registry import get_provider, parse_model_string
from ..types import CompletionRequest, Finding, Message, Usage

_CANON_DIGEST_CHARS = 8000


@dataclass
class ReviseResult:
    """Outcome of one `revise_chapter` call."""

    chapter: int
    old_words: int
    new_words: int
    applied: int
    summary: str
    usage: Usage


_BEGIN_RE = re.compile(r"BEGIN\s+CHAPTER\s*\n?", re.IGNORECASE)
_END_RE = re.compile(r"\n?\s*END\s+CHAPTER", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"^\s*SUMMARY\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _extract_revised_body(text: str) -> str:
    """Tolerantly pull the chapter body from between the sentinels.

    Falls back to "everything after BEGIN CHAPTER" if END is missing, and
    to the whole (trimmed) response if neither sentinel is present -- a
    model that forgets the wrapper but still returns plain prose shouldn't
    lose the revision.
    """
    begin_m = _BEGIN_RE.search(text)
    if begin_m is None:
        return text.strip()
    end_m = _END_RE.search(text, begin_m.end())
    if end_m is not None and end_m.start() > begin_m.end():
        return text[begin_m.end() : end_m.start()].strip("\n")
    return text[begin_m.end() :].strip("\n")


def _extract_summary(text: str, default: str) -> str:
    """Look for a `SUMMARY:` line, preferring one before BEGIN CHAPTER so a
    line inside the revised prose itself isn't mistaken for it."""
    begin_m = _BEGIN_RE.search(text)
    head = text[: begin_m.start()] if begin_m else text
    m = _SUMMARY_RE.search(head)
    if m:
        return m.group(1).strip()
    return default


def _build_revision_prompt(
    chapter: int,
    body: str,
    findings: list[Finding],
    canon_digest: str,
    style_excerpt: str,
    banned_words: list[str],
    banned_phrases: list[str],
) -> tuple[str, str]:
    system = (
        "You are a meticulous line editor revising a single chapter of a "
        "novel-in-progress. Apply every accepted finding precisely, "
        "preserve the author's established voice and plot, and change "
        "nothing beyond what the findings require."
    )
    findings_lines = []
    for f in findings:
        line = f"- [{f.severity.value}] ({f.source}/{f.category}) {f.issue}"
        if f.quote:
            line += f' -- quote: "{f.quote}"'
        if f.suggestion:
            line += f" -- suggestion: {f.suggestion}"
        findings_lines.append(line)

    parts = [f"## Chapter {chapter} (original, full text)\n\n{body}"]
    if canon_digest:
        parts.append(f"## Canon\n\n{canon_digest}")
    if style_excerpt:
        parts.append(f"## Style Guide\n\n{style_excerpt}")
    if banned_words or banned_phrases:
        banned = ", ".join(banned_words + banned_phrases)
        parts.append(f"## Banned words/phrases (avoid entirely)\n\n{banned}")
    parts.append("## Accepted findings to address\n\n" + "\n".join(findings_lines))
    parts.append(
        "## Task\n\n"
        "Rewrite the ENTIRE chapter, incorporating a fix for every finding "
        "above. Respond with ONLY the following, in exactly this format "
        "(no other prose):\n\n"
        "SUMMARY: <one paragraph describing what you changed and why>\n"
        "BEGIN CHAPTER\n"
        "<the complete revised chapter body>\n"
        "END CHAPTER"
    )
    return system, "\n\n".join(parts)


def revise_chapter(
    project: WritingProject,
    chapter: int,
    findings: list[Finding],
    model: str | None = None,
    provider: Provider | None = None,
) -> ReviseResult:
    """Revise `chapter` by applying `findings` (already filtered to
    "accepted" by the caller) via the model. Raises `ValueError` if
    `findings` is empty -- revision without findings is refused rather
    than silently rewriting the chapter for no reason."""
    if not findings:
        raise ValueError("revise_chapter requires at least one finding to apply")

    model_str = model if model is not None else project.config.models.reviewer
    if provider is not None:
        prov = provider
        model_id = parse_model_string(model_str)[1] if "/" in model_str else model_str
    else:
        prov, model_id = get_provider(model_str, project.config)

    fm, body = project.read_chapter(chapter)
    old_words = count_words(body)

    store = CanonStore(project)
    canon_digest = store.context_pack(max_chars=_CANON_DIGEST_CHARS)
    style_excerpt = store.style_body_without_banned()
    banned_words, banned_phrases = store.banned_terms()

    system, user = _build_revision_prompt(
        chapter, body, findings, canon_digest, style_excerpt, banned_words, banned_phrases
    )
    req = CompletionRequest(
        model=model_id,
        system=system,
        messages=[Message(role="user", content=user)],
        max_tokens=project.config.max_tokens,
        temperature=project.config.temperature,
    )
    resp = prov.complete(req)

    revised_body = _extract_revised_body(resp.text)
    old_word_count = count_words(body)
    new_word_count = count_words(revised_body)
    # Never let a truncated/empty model response destroy a chapter: a real
    # revision is prose of comparable length, not a stub.
    if new_word_count == 0 or new_word_count < old_word_count // 4:
        raise ValueError(
            f"Revision for chapter {chapter} came back with {new_word_count} "
            f"words (original: {old_word_count}); refusing to overwrite. "
            "The model response was likely truncated — raise max_tokens in "
            "stoner.yaml or retry. The original chapter is untouched."
        )
    default_summary = f"Applied {len(findings)} finding(s) to chapter {chapter}."
    summary = _extract_summary(resp.text, default_summary)

    new_fm = dict(fm)
    new_fm["status"] = "revised"
    project.write_chapter(chapter, new_fm, revised_body)

    new_words = count_words(revised_body)

    Ledger(project.root).append(
        "review.revise",
        target=project.chapter_rel(chapter),
        chapter=chapter,
        applied=len(findings),
        old_words=old_words,
        new_words=new_words,
    )

    return ReviseResult(
        chapter=chapter,
        old_words=old_words,
        new_words=new_words,
        applied=len(findings),
        summary=summary,
        usage=resp.usage,
    )
