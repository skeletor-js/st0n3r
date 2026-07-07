"""Built-in writing tools exposed to the agent, bound to a `WritingProject`.

All filesystem access goes through `WritingProject` so tools stay path-jailed
to the project root. Every tool function has the shape
`(project: WritingProject, **kwargs) -> str` and never raises: failures come
back as `"ERROR: ..."` strings so the agent loop can keep going.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..project import ProjectError, WritingProject, count_words
from ..types import ToolCall, ToolResult, ToolSpec

ToolFunc = Callable[..., str]

_SEARCH_MAX_RESULTS = 40

_SCOPE_DIRS: dict[str, list[str]] = {
    "manuscript": ["manuscript"],
    "canon": ["canon"],
    "outline": ["outline"],
    "all": ["manuscript", "canon", "outline"],
}


def _as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} must be an integer, got {value!r}") from e


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def read_chapter(project: WritingProject, number: Any) -> str:
    try:
        n = _as_int(number, "number")
        fm, body = project.read_chapter(n)
    except (ProjectError, ValueError) as e:
        return f"ERROR: {e}"
    header = json.dumps(fm, ensure_ascii=False)
    return f"# ch-{n:02d} frontmatter: {header}\n\n{body}"


def write_chapter(
    project: WritingProject,
    number: Any,
    title: str = "",
    body: str = "",
    status: str = "",
    pov: str = "",
) -> str:
    try:
        n = _as_int(number, "number")
        if not body.strip():
            return "ERROR: body must not be empty"
        # Merge with any existing frontmatter so a redraft doesn't silently
        # drop pov/status or other keys set by earlier stages.
        try:
            fm, _old_body = project.read_chapter(n)
            fm = dict(fm)
        except ProjectError:
            fm = {}
        if title:
            fm["title"] = title
        fm["status"] = status or fm.get("status") or "draft"
        if pov:
            fm["pov"] = pov
        path = project.write_chapter(n, fm, body)
    except (ProjectError, ValueError) as e:
        return f"ERROR: {e}"
    words = count_words(body)
    return f"Wrote {path.relative_to(project.root)} ({words} words, status={fm['status']})"


def list_project(project: WritingProject) -> str:
    try:
        summary = project.status_summary()
        chapters = [
            {
                "number": c.number,
                "title": c.title,
                "status": c.status,
                "pov": c.pov,
                "words": c.words,
            }
            for c in sorted(project.chapters(), key=lambda c: c.number)
        ]
    except ProjectError as e:
        return f"ERROR: {e}"
    return json.dumps({"summary": summary, "chapters": chapters}, ensure_ascii=False, indent=2)


def search_text(project: WritingProject, query: str, scope: str = "all") -> str:
    if not query:
        return "ERROR: query must not be empty"
    scope = (scope or "all").lower()
    dirs = _SCOPE_DIRS.get(scope)
    if dirs is None:
        return f"ERROR: unknown scope {scope!r}; expected one of {sorted(_SCOPE_DIRS)}"
    needle = query.lower()
    matches: list[str] = []
    truncated = False
    for d in dirs:
        base = project.root / d
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = path.relative_to(project.root)
            for i, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    if len(matches) >= _SEARCH_MAX_RESULTS:
                        truncated = True
                        break
                    matches.append(f"{rel}:{i}: {line.strip()}")
            if truncated:
                break
        if truncated:
            break
    if not matches:
        return f"No matches for {query!r} in scope={scope}"
    out = "\n".join(matches)
    if truncated:
        out += f"\n[truncated at {_SEARCH_MAX_RESULTS} results]"
    return out


def query_canon(project: WritingProject, topic: str = "") -> str:
    canon_dir = project.root / "canon"
    if not canon_dir.exists():
        return "ERROR: no canon/ directory in this project"
    files = sorted(p for p in canon_dir.rglob("*.md") if p.is_file())
    if not topic:
        rels = [str(p.relative_to(project.root)) for p in files]
        return "Canon files:\n" + "\n".join(rels) if rels else "No canon files found."
    needle = topic.strip().lower().replace(" ", "-")
    for p in files:
        stem = p.stem.lower()
        rel = str(p.relative_to(project.root)).lower()
        if needle == stem or needle in rel:
            try:
                return p.read_text(encoding="utf-8")
            except OSError as e:
                return f"ERROR: could not read {p}: {e}"
    return f"ERROR: no canon file matching {topic!r}. Use query_canon() with no topic to list files."


def update_canon(project: WritingProject, path: str, content: str) -> str:
    norm = path.replace("\\", "/").lstrip("/")
    if not (norm == "canon" or norm.startswith("canon/")):
        return f"ERROR: path must be under canon/, got {path!r}"
    try:
        written = project.write(norm, content)
    except ProjectError as e:
        return f"ERROR: {e}"
    return f"Updated {written.relative_to(project.root)}"


def read_outline(project: WritingProject, chapter: Any = None) -> str:
    try:
        if chapter is None or chapter == "":
            return project.read("outline/outline.md")
        n = _as_int(chapter, "chapter")
        return project.read(f"outline/beats/ch-{n:02d}.md")
    except (ProjectError, ValueError) as e:
        return f"ERROR: {e}"


def update_beats(project: WritingProject, chapter: Any, content: str) -> str:
    try:
        n = _as_int(chapter, "chapter")
        path = project.write(f"outline/beats/ch-{n:02d}.md", content)
    except (ProjectError, ValueError) as e:
        return f"ERROR: {e}"
    return f"Updated {path.relative_to(project.root)}"


def get_memory(project: WritingProject) -> str:
    return json.dumps(project.read_memory(), ensure_ascii=False, indent=2)


def slop_check(project: WritingProject, chapter: Any) -> str:
    """Run the deterministic slop detector on a chapter (self-serve for agents)."""
    try:
        n = _as_int(chapter, "chapter")
        _fm, body = project.read_chapter(n)
    except (ProjectError, ValueError) as e:
        return f"ERROR: {e}"
    from ..canon.store import CanonStore
    from ..slop import run_slop
    from ..slop.report import verdict

    bw, bp = CanonStore(project).banned_terms()
    report = run_slop(body, path=project.chapter_rel(n), banned_words=bw, banned_phrases=bp)
    worst = sorted(
        report.findings,
        key=lambda f: {"critical": 0, "major": 1, "minor": 2, "info": 3}.get(f.severity.value, 4),
    )[:15]
    lines = [
        f"slop score: {report.score:.1f}/100 ({verdict(report.score)}); "
        f"{len(report.findings)} findings, worst first:"
    ]
    for f in worst:
        loc = f"L{f.span.line}" if f.span else "-"
        lines.append(f"- [{f.severity.value}] {loc} {f.issue}")
    if len(report.findings) > len(worst):
        lines.append(f"...and {len(report.findings) - len(worst)} more.")
    return "\n".join(lines)


def voice_check(project: WritingProject, chapter: Any) -> str:
    """Run the deterministic voice-drift check on a chapter (self-serve)."""
    try:
        n = _as_int(chapter, "chapter")
        _fm, body = project.read_chapter(n)
    except (ProjectError, ValueError) as e:
        return f"ERROR: {e}"
    from ..voice.drift import run_voice
    from ..voice.fingerprint import FingerprintError, load_fingerprint
    from ..voice.report import verdict

    try:
        fingerprint = load_fingerprint(project)
    except FingerprintError as e:
        return f"ERROR: no fingerprint -- {e}"
    report = run_voice(body, fingerprint, path=project.chapter_rel(n), config=project.config.voice)
    worst = sorted(
        report.findings,
        key=lambda f: {"critical": 0, "major": 1, "minor": 2, "info": 3}.get(f.severity.value, 4),
    )[:10]
    lines = [
        f"voice drift: {report.score:.1f}/100 ({verdict(report.score)}); "
        f"{len(report.findings)} drifting passage(s), worst first:"
    ]
    for f in worst:
        loc = f"L{f.span.line}" if f.span else "-"
        lines.append(f"- [{f.severity.value}] {loc} {f.issue} -- {f.suggestion}")
    if len(report.findings) > len(worst):
        lines.append(f"...and {len(report.findings) - len(worst)} more.")
    return "\n".join(lines)


def word_count(project: WritingProject, chapter: Any = None) -> str:
    try:
        if chapter is None or chapter == "":
            chapters = project.chapters()
            total = sum(c.words for c in chapters)
            per = {f"ch-{c.number:02d}": c.words for c in sorted(chapters, key=lambda c: c.number)}
            return json.dumps({"total": total, "by_chapter": per}, ensure_ascii=False, indent=2)
        n = _as_int(chapter, "chapter")
        _fm, body = project.read_chapter(n)
        return f"ch-{n:02d}: {count_words(body)} words"
    except (ProjectError, ValueError) as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Binds `ToolSpec` definitions to Python callables and executes calls
    against a `WritingProject`, always returning a `ToolResult` (never
    raising)."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._funcs: dict[str, ToolFunc] = {}

    def register(self, spec: ToolSpec, func: ToolFunc) -> None:
        self._specs[spec.name] = spec
        self._funcs[spec.name] = func

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def execute(self, project: WritingProject, call: ToolCall) -> ToolResult:
        func = self._funcs.get(call.name)
        if func is None:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"ERROR: unknown tool {call.name!r}. Available: {', '.join(self.names())}",
                is_error=True,
            )
        try:
            content = func(project, **call.arguments)
        except Exception as e:  # noqa: BLE001 - tool executor must never raise
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"ERROR: tool {call.name!r} raised {type(e).__name__}: {e}",
                is_error=True,
            )
        is_error = isinstance(content, str) and content.startswith("ERROR:")
        return ToolResult(call_id=call.id, name=call.name, content=content, is_error=is_error)


def default_registry() -> ToolRegistry:
    """Build the registry of built-in writing tools."""
    reg = ToolRegistry()

    reg.register(
        ToolSpec(
            name="read_chapter",
            description="Read a manuscript chapter's frontmatter and body by chapter number.",
            parameters={
                "type": "object",
                "properties": {"number": {"type": "integer", "description": "Chapter number"}},
                "required": ["number"],
            },
        ),
        read_chapter,
    )
    reg.register(
        ToolSpec(
            name="write_chapter",
            description=(
                "Write (create or overwrite) a manuscript chapter. Always call this to "
                "produce prose output; word count and frontmatter are maintained automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "title": {"type": "string"},
                    "body": {"type": "string", "description": "Full chapter prose in markdown."},
                    "status": {
                        "type": "string",
                        "enum": ["outline", "draft", "revised", "final"],
                        "default": "draft",
                    },
                    "pov": {"type": "string"},
                },
                "required": ["number", "title", "body"],
            },
        ),
        write_chapter,
    )
    reg.register(
        ToolSpec(
            name="list_project",
            description="List project status: chapter count, word totals, per-chapter status.",
            parameters={"type": "object", "properties": {}},
        ),
        list_project,
    )
    reg.register(
        ToolSpec(
            name="search_text",
            description="Search project markdown files for a substring; returns path:line matches.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["manuscript", "canon", "outline", "all"],
                        "default": "all",
                    },
                },
                "required": ["query"],
            },
        ),
        search_text,
    )
    reg.register(
        ToolSpec(
            name="query_canon",
            description=(
                "List canon files, or read one canon file by topic/slug (matches filename or "
                "path substring). Always check canon before inventing facts."
            ),
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string", "default": ""}},
            },
        ),
        query_canon,
    )
    reg.register(
        ToolSpec(
            name="update_canon",
            description="Update (or create) a canon file. `path` must be under canon/.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "e.g. canon/characters/mira.md"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        ),
        update_canon,
    )
    reg.register(
        ToolSpec(
            name="read_outline",
            description="Read the overall outline, or one chapter's beat sheet if `chapter` given.",
            parameters={
                "type": "object",
                "properties": {"chapter": {"type": "integer"}},
            },
        ),
        read_outline,
    )
    reg.register(
        ToolSpec(
            name="update_beats",
            description="Write a chapter's beat sheet (outline/beats/ch-NN.md).",
            parameters={
                "type": "object",
                "properties": {
                    "chapter": {"type": "integer"},
                    "content": {"type": "string"},
                },
                "required": ["chapter", "content"],
            },
        ),
        update_beats,
    )
    reg.register(
        ToolSpec(
            name="get_memory",
            description="Read the rolling memory summaries (.stoner/memory.json).",
            parameters={"type": "object", "properties": {}},
        ),
        get_memory,
    )
    reg.register(
        ToolSpec(
            name="word_count",
            description="Word count for one chapter, or totals across all chapters if omitted.",
            parameters={"type": "object", "properties": {"chapter": {"type": "integer"}}},
        ),
        word_count,
    )
    reg.register(
        ToolSpec(
            name="slop_check",
            description=(
                "Run the deterministic AI-slop detector on a chapter you have "
                "written; returns a 0-100 score and the worst findings. Use it "
                "to self-check before finishing."
            ),
            parameters={
                "type": "object",
                "properties": {"chapter": {"type": "integer"}},
                "required": ["chapter"],
            },
        ),
        slop_check,
    )
    reg.register(
        ToolSpec(
            name="voice_check",
            description=(
                "Run the deterministic voice-drift check on a chapter you have "
                "written; returns a 0-100 drift score (0 = in voice) and the "
                "worst drifting passages. Returns an error string when no "
                "fingerprint has been learned yet."
            ),
            parameters={
                "type": "object",
                "properties": {"chapter": {"type": "integer"}},
                "required": ["chapter"],
            },
        ),
        voice_check,
    )
    return reg
