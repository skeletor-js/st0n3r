"""run_review: execute critic passes sequentially and save a ReviewReport.

One provider instance runs every requested pass against a single chapter,
accumulating `Usage` and tolerating individual pass failures (a provider
error or unparseable response becomes an `info`-severity Finding rather
than aborting the whole review) so one flaky pass never loses the others'
results. The report is saved as JSON + a human-readable Markdown rendering
under `.stoner/reviews/`, and the run is recorded in the ledger.
"""

from __future__ import annotations

import time

from ..config import StonerConfig
from ..ledger import Ledger
from ..project import WritingProject
from ..providers.base import Provider, ProviderError
from ..providers.registry import get_provider, parse_model_string
from ..types import CompletionRequest, Finding, Message, ReviewReport, Severity, Usage
from .passes import PASSES, build_context, extract_json, locate_span

_MAX_FINDINGS_PER_PASS = 25


def _resolve_provider(model: str, config: StonerConfig, provider: Provider | None) -> tuple[Provider, str]:
    """Return (provider, bare model id). `provider` overrides construction
    (test injection); `model` is still split for the bare id sent to it."""
    if provider is not None:
        model_id = parse_model_string(model)[1] if "/" in model else model
        return provider, model_id
    return get_provider(model, config)


def _run_one_pass(
    name: str,
    provider: Provider,
    model_id: str,
    project: WritingProject,
    ctx,
) -> tuple[list[Finding], str, Usage]:
    """Run a single named pass. Never raises: failures become an info Finding."""
    source = f"review:{name}"
    pass_obj = PASSES.get(name)
    if pass_obj is None:
        return (
            [Finding(source=source, severity=Severity.info, issue=f"pass failed: unknown pass {name!r}")],
            "",
            Usage(),
        )
    try:
        system, user = pass_obj.build_prompt(ctx)
        req = CompletionRequest(
            model=model_id,
            system=system,
            messages=[Message(role="user", content=user)],
            max_tokens=project.config.max_tokens,
            temperature=project.config.temperature,
        )
        resp = provider.complete(req)
        data = extract_json(resp.text)
        if not data:
            raise ValueError("model response did not contain parseable JSON")
        findings = pass_obj.parse(resp.text)
        for f in findings:
            if f.quote and f.span is None:
                f.span = locate_span(ctx.chapter_body, f.quote)
        summary = str(data.get("summary", "")).strip()
        return findings[:_MAX_FINDINGS_PER_PASS], summary, resp.usage
    except ProviderError as e:
        return [Finding(source=source, severity=Severity.info, issue=f"pass failed: {e}")], "", Usage()
    except Exception as e:  # noqa: BLE001 - any pass failure degrades to a Finding, never aborts the run
        return [Finding(source=source, severity=Severity.info, issue=f"pass failed: {e}")], "", Usage()


def _render_markdown(report: ReviewReport) -> str:
    order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
    findings = sorted(report.findings, key=lambda f: order.get(f.severity.value, 4))
    lines = [
        f"# Review report: {report.path}",
        "",
        f"**Model:** {report.model}  ",
        f"**Passes:** {', '.join(report.passes)}",
        "",
        "## Summary",
        "",
        report.summary or "_No summary._",
        "",
        f"## Findings ({len(findings)})",
        "",
    ]
    if not findings:
        lines.append("_No findings._")
    for f in findings:
        loc = f"L{f.span.line}" if f.span else "-"
        quote = f' -- `{f.quote}`' if f.quote else ""
        lines.append(f"- **[{f.severity.value}]** ({f.source}/{f.category}) {loc}: {f.issue}{quote}")
        if f.suggestion:
            lines.append(f"  - suggestion: {f.suggestion}")
    lines += [
        "",
        "## Usage",
        "",
        f"- input_tokens: {report.usage.input_tokens}",
        f"- output_tokens: {report.usage.output_tokens}",
    ]
    return "\n".join(lines)


def run_review(
    project: WritingProject,
    chapter: int,
    passes: list[str] | None = None,
    model: str | None = None,
    provider: Provider | None = None,
) -> ReviewReport:
    """Run critic passes over `chapter` and save the resulting ReviewReport.

    `passes` defaults to `project.config.review_passes`; `model` defaults to
    `project.config.models.reviewer`. `provider`, if given, replaces the
    provider that would otherwise be constructed via `get_provider` -- the
    hook tests use to inject a scripted provider.
    """
    pass_names = list(passes) if passes is not None else list(project.config.review_passes)
    model_str = model if model is not None else project.config.models.reviewer

    prov, model_id = _resolve_provider(model_str, project.config, provider)
    ctx = build_context(project, chapter)

    findings: list[Finding] = []
    summaries: list[str] = []
    usage = Usage()

    for name in pass_names:
        pass_findings, summary, pass_usage = _run_one_pass(name, prov, model_id, project, ctx)
        findings.extend(pass_findings)
        usage = usage + pass_usage
        if summary:
            summaries.append(f"### {name}\n\n{summary}")

    rel_path = project.chapter_rel(chapter)
    report = ReviewReport(
        path=rel_path,
        passes=pass_names,
        findings=findings,
        summary="\n\n".join(summaries),
        model=model_str,
        usage=usage,
    )

    ts = int(time.time())
    base = f".stoner/reviews/ch-{chapter:02d}-{ts}"
    project.write(f"{base}.json", report.model_dump_json(indent=2))
    project.write(f"{base}.md", _render_markdown(report))

    Ledger(project.root).append(
        "review.run",
        target=rel_path,
        chapter=chapter,
        passes=pass_names,
        model=model_str,
        findings=len(findings),
    )

    return report
