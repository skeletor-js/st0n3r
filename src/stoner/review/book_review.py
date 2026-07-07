"""run_book_review: the whole-manuscript critic pass (autonovel's Opus loop).

Unlike `runner.run_review`, which critiques one chapter with the per-chapter
context pack, this assembles the *entire* book (respecting context limits)
and asks the reviewer, in one pass, to read it first as a literary critic and
then as a professor of fiction, returning STRICT JSON findings tagged with
the chapter each belongs to. The harness maps findings back to chapters,
locates quotes within each chapter body, saves a JSON + Markdown report under
`.stoner/reviews/`, and returns a `BookReviewReport` whose per-chapter
grouping drives the revise cycles in `pipelines/book.py`.

Assembly respects the "never read the whole manuscript when it is large"
rule (`docs/planning/ARCHITECTURE.md`): at most `_FULL_TEXT_LIMIT` chapters
are sent verbatim; beyond that only the `_RECENT_FULL` most recent chapters
are full text and the rest are represented by their rolling memory summaries.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ..canon.memory import Memory
from ..ledger import Ledger
from ..pipelines.common import call_model, render_prompt
from ..project import WritingProject
from ..providers.base import Provider
from ..types import Finding, Severity, Usage
from .passes import extract_json, locate_span

# When the book has this many chapters or fewer, send every chapter in full;
# beyond it, only the most recent `_RECENT_FULL` are full text and the rest
# are summarized from rolling memory.
_FULL_TEXT_LIMIT = 12
_RECENT_FULL = 6

_MAJOR_SEVERITIES = (Severity.major, Severity.critical)
_CH_CATEGORY_RE = re.compile(r"ch-0*(\d+):")


def finding_chapter(finding: Finding) -> int | None:
    """Recover the chapter number a book-review finding was tagged with.

    Book findings carry the chapter in a `ch-NN:` category prefix so the
    shared `Finding` model needs no extra field; this reverses that."""
    m = _CH_CATEGORY_RE.match(finding.category)
    return int(m.group(1)) if m else None


def _coerce_severity(value: object) -> Severity:
    try:
        return Severity(str(value).strip().lower())
    except ValueError:
        return Severity.major


@dataclass
class BookReviewReport:
    """Result of one whole-manuscript review pass."""

    findings: list[Finding] = field(default_factory=list)
    overall: str = ""
    verdict: str = "needs-work"
    model: str = ""
    chapters_full: list[int] = field(default_factory=list)
    chapters_summarized: list[int] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    created_at: float = field(default_factory=time.time)
    json_path: str = ""
    md_path: str = ""

    def by_chapter(self) -> dict[int, list[Finding]]:
        """Group findings by the chapter they were tagged with (untagged
        findings — chapter 0/None — are dropped from the mapping)."""
        out: dict[int, list[Finding]] = defaultdict(list)
        for f in self.findings:
            n = finding_chapter(f)
            if n:
                out[n].append(f)
        return dict(out)

    @property
    def major_count(self) -> int:
        """Findings a revise cycle should act on: major and critical."""
        return sum(1 for f in self.findings if f.severity in _MAJOR_SEVERITIES)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.critical)


def _assemble_manuscript(
    project: WritingProject,
) -> tuple[str, dict[int, str], list[int], list[int]]:
    """Return (assembled text, {n: body}, full-text chapter nums, summarized
    chapter nums). Short books go in whole; long ones send recent chapters in
    full and summarize the rest from rolling memory."""
    chapters = project.chapters()
    numbers = [c.number for c in chapters]
    bodies: dict[int, str] = {}
    for c in chapters:
        try:
            _, bodies[c.number] = project.read_chapter(c.number)
        except Exception:  # noqa: BLE001 - a missing/broken chapter file is simply skipped
            bodies[c.number] = ""

    memory = Memory(project)
    full: list[int] = []
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


def _parse_report(text: str, bodies: dict[int, str], model_str: str) -> tuple[list[Finding], str, str]:
    data = extract_json(text)
    findings: list[Finding] = []
    raw = data.get("findings")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("chapter"))
            except (TypeError, ValueError):
                n = 0
            category = str(item.get("category", "")).strip()
            quote = str(item.get("quote", ""))
            finding = Finding(
                source="review:book",
                severity=_coerce_severity(item.get("severity", "major")),
                category=f"ch-{n:02d}:{category}",
                quote=quote,
                issue=str(item.get("issue", "")),
                suggestion=str(item.get("suggestion", "")),
            )
            body = bodies.get(n)
            if quote and body:
                finding.span = locate_span(body, quote)
            findings.append(finding)
    overall = str(data.get("overall", "")).strip()
    verdict = str(data.get("verdict", "needs-work")).strip().lower()
    if verdict not in ("ready", "needs-work"):
        verdict = "ready" if not findings else "needs-work"
    return findings, overall, verdict


def _render_markdown(report: BookReviewReport) -> str:
    order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
    lines = [
        "# Whole-manuscript review",
        "",
        f"**Model:** {report.model}  ",
        f"**Verdict:** {report.verdict}  ",
        f"**Full-text chapters:** {report.chapters_full or '-'}  ",
        f"**Summarized chapters:** {report.chapters_summarized or '-'}",
        "",
        "## Overall",
        "",
        report.overall or "_No overall verdict._",
        "",
        f"## Findings ({len(report.findings)})",
        "",
    ]
    if not report.findings:
        lines.append("_No findings._")
    grouped = report.by_chapter()
    for n in sorted(grouped):
        lines.append(f"### Chapter {n}")
        lines.append("")
        for f in sorted(grouped[n], key=lambda f: order.get(f.severity.value, 4)):
            loc = f"L{f.span.line}" if f.span else "-"
            quote = f' -- `{f.quote}`' if f.quote else ""
            cat = f.category.split(":", 1)[-1]
            lines.append(f"- **[{f.severity.value}]** ({cat}) {loc}: {f.issue}{quote}")
            if f.suggestion:
                lines.append(f"  - suggestion: {f.suggestion}")
        lines.append("")
    lines += [
        "## Usage",
        "",
        f"- input_tokens: {report.usage.input_tokens}",
        f"- output_tokens: {report.usage.output_tokens}",
    ]
    return "\n".join(lines)


def run_book_review(
    project: WritingProject,
    model: str | None = None,
    provider: Provider | None = None,
) -> BookReviewReport:
    """Review the whole manuscript in one pass and save a report.

    `model` overrides the reviewer role; `provider`, if given, replaces the
    constructed provider (test injection). Raises `ValueError` when there are
    no chapters to review.
    """
    if not project.chapters():
        raise ValueError("No chapters to review — draft some chapters first (stoner book).")

    manuscript, bodies, full, summarized = _assemble_manuscript(project)
    model_str = model if model is not None else project.config.models.reviewer

    system = render_prompt("book_review.md", {"project_name": project.config.project_name})
    user = (
        "Review the complete manuscript below and return the JSON described "
        "in your instructions.\n\n" + manuscript
    )
    text, usage = call_model(
        project, "reviewer", system=system, user=user, model=model, provider=provider
    )

    findings, overall, verdict = _parse_report(text, bodies, model_str)
    report = BookReviewReport(
        findings=findings,
        overall=overall,
        verdict=verdict,
        model=model_str,
        chapters_full=full,
        chapters_summarized=summarized,
        usage=usage,
    )

    ts = int(time.time())
    base = f".stoner/reviews/book-{ts}"
    payload = {
        "created_at": report.created_at,
        "model": report.model,
        "verdict": report.verdict,
        "overall": report.overall,
        "chapters_full": report.chapters_full,
        "chapters_summarized": report.chapters_summarized,
        "usage": report.usage.model_dump(),
        "findings": [f.model_dump(mode="json") for f in report.findings],
    }
    report.json_path = str(project.write(f"{base}.json", json.dumps(payload, indent=2, ensure_ascii=False)))
    report.md_path = str(project.write(f"{base}.md", _render_markdown(report)))

    Ledger(project.root).append(
        "review.book",
        target=f"{base}.json",
        model=model_str,
        findings=len(findings),
        majors=report.major_count,
        verdict=verdict,
    )
    return report
