"""Model-drafted synopsis, query letter, and cover brief (R10, R11).

Three `call_model` calls against the `writer` role over a single shared
context: the canon context pack, the rolling memory (book-so-far plus every
chapter summary in order), open/resolved threads, and the premise's logline /
Genre & Comps / Promise sections -- *never* the manuscript prose itself
(invariant 4). Each result is saved verbatim as markdown in `export/`; these
are the human's drafts, never machine-edited afterward. Regeneration refuses
to overwrite an existing file without `--force` (invariant 11).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..canon.memory import Memory
from ..canon.store import CanonStore, extract_section
from ..ledger import Ledger
from ..pipelines.common import call_model, render_prompt
from ..project import WritingProject, count_words
from ..providers.base import Provider
from ..types import Usage
from .manifest import build_manifest

# artifact -> (output filename, prompt template, ledger action, persona)
_ARTIFACTS: dict[str, tuple[str, str, str, str]] = {
    "synopsis": (
        "synopsis.md",
        "ship_synopsis.md",
        "ship.synopsis",
        "You are a publishing professional drafting a complete, spoiler-full "
        "novel synopsis for an agent or acquisitions editor.",
    ),
    "query": (
        "query-letter.md",
        "ship_query.md",
        "ship.query",
        "You are an author drafting a one-page query letter to a literary agent.",
    ),
    "cover": (
        "cover-brief.md",
        "ship_cover.md",
        "ship.cover",
        "You are an art director writing a cover-design brief for a book "
        "designer. You describe mood and imagery; you do not generate an image.",
    ),
}

#: The public order artifacts are generated in.
ARTIFACT_ORDER = ("synopsis", "query", "cover")


@dataclass
class BlurbResult:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def build_blurb_context(project: WritingProject) -> dict[str, str]:
    """Assemble the shared blurb context. Never includes chapter prose."""
    store = CanonStore(project)
    manifest = build_manifest(project)

    memory = Memory(project)
    mem_parts: list[str] = []
    if memory.book_so_far:
        mem_parts.append(f"Book so far:\n{memory.book_so_far}")
    for ref in manifest.chapters:
        summary = memory.get_chapter_summary(ref.number)
        if summary:
            mem_parts.append(f"Chapter {ref.number} — {ref.title}: {summary}")
    memory_text = "\n\n".join(mem_parts)

    thread_lines = [
        f"- ({t.id}, {t.status}) {t.thread}"
        for t in store.threads()
        if t.status in ("open", "resolved")
    ]

    premise = store.get("canon/premise.md")
    premise_body = premise.body if premise else ""

    total_words = 0
    for ref in manifest.chapters:
        _fm, body = project.read_chapter(ref.number)
        total_words += count_words(body)

    return {
        "title": manifest.title,
        "author": manifest.author,
        "word_count": f"{total_words:,}",
        "logline": extract_section(premise_body, "Logline"),
        "comps": extract_section(premise_body, "Genre & Comps"),
        "promise": extract_section(premise_body, "Promise to the Reader"),
        "canon": store.context_pack(max_chars=8000),
        "memory": memory_text,
        "threads": "\n".join(thread_lines),
    }


def run_blurbs(
    project: WritingProject,
    only: str | None = None,
    force: bool = False,
    model: str | None = None,
    provider: Provider | None = None,
) -> BlurbResult:
    """Draft the requested blurb artifacts. Skips existing files unless
    `force`; a provider error propagates and leaves no partial file behind."""
    if only is not None and only not in _ARTIFACTS:
        raise ValueError(
            f"unknown blurb artifact {only!r} (expected one of {', '.join(ARTIFACT_ORDER)})"
        )
    wanted = [only] if only else list(ARTIFACT_ORDER)
    context = build_blurb_context(project)
    ledger = Ledger(project.root)
    result = BlurbResult()

    for name in wanted:
        filename, template, action, persona = _ARTIFACTS[name]
        rel = f"export/{filename}"
        dest = project.resolve(rel)
        if dest.exists() and not force:
            result.skipped.append(rel)
            continue
        user = render_prompt(template, context)
        text, usage = call_model(
            project, "writer", persona, user, model=model, provider=provider
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        result.usage = result.usage + usage
        result.written.append(rel)
        ledger.append(
            action,
            target=rel,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
    return result
