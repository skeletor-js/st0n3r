"""st0n3r command-line interface."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .. import __version__
from ..config import CONFIG_FILENAME
from ..ledger import Ledger
from ..project import ProjectError, WritingProject
from ..providers.base import ProviderError
from ..types import Finding, ReviewReport

app = typer.Typer(
    name="stoner",
    help="st0n3r — an AI harness purpose-built for long-form writing.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
canon_app = typer.Typer(help="Inspect and search the canon (the story bible).", no_args_is_help=True)
app.add_typer(canon_app, name="canon")
chapter_app = typer.Typer(help="Create and import manuscript chapters (no API key needed).", no_args_is_help=True)
app.add_typer(chapter_app, name="chapter")

console = Console()
err_console = Console(stderr=True, style="bold red")


def _project() -> WritingProject:
    try:
        return WritingProject.find()
    except ProjectError as e:
        err_console.print(str(e))
        raise typer.Exit(1) from None


def _fail(msg: str) -> None:
    err_console.print(msg)
    raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def _root(
    version: bool = typer.Option(False, "--version", help="Show version and exit."),
) -> None:
    if version:
        console.print(f"st0n3r {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# init / status
# ---------------------------------------------------------------------------


@app.command()
def init(
    name: str = typer.Argument(..., help="Project name (also the directory unless --path)."),
    path: Path = typer.Option(None, help="Directory to create the project in."),
) -> None:
    """Create a new writing project with the opinionated canon structure."""
    from ..canon.scaffold import scaffold_project

    root = (path or Path(name)).resolve()
    try:
        project = WritingProject.create(root, name)
    except ProjectError as e:
        _fail(str(e))
    created = scaffold_project(project, name)
    Ledger(project.root).append("project.init", target=str(root))
    console.print(f"[green]Created project[/green] [bold]{name}[/bold] at {root}")
    console.print(f"  {len(created)} starter files written. Begin with canon/premise.md.")
    console.print(f"  Configure models in {CONFIG_FILENAME}, then try: stoner status")


@app.command()
def status() -> None:
    """Project overview: chapters, words, recent activity."""
    project = _project()
    s = project.status_summary()
    console.print(f"[bold]{s['name']}[/bold] — {s['root']}")
    console.print(f"chapters: {s['chapters']}   words: {s['words']:,}")
    if s["by_status"]:
        console.print("   " + "  ".join(f"{k}: {v}" for k, v in s["by_status"].items()))
    table = Table(title="chapters", show_lines=False)
    for col in ("ch", "title", "status", "pov", "words"):
        table.add_column(col)
    for c in project.chapters():
        table.add_row(f"{c.number:02d}", c.title, c.status, c.pov, f"{c.words:,}")
    if project.chapters():
        console.print(table)
    recent = Ledger(project.root).tail(5)
    if recent:
        console.print("[dim]recent:[/dim]")
        for e in recent:
            console.print(f"  [dim]{e.action} {e.target}[/dim]")


# ---------------------------------------------------------------------------
# slop
# ---------------------------------------------------------------------------


@app.command()
def slop(
    target: str = typer.Argument(..., help="Chapter number, a file path, or 'all'."),
    fmt: str = typer.Option("rich", help="Output format: rich | markdown | json."),
    save: bool = typer.Option(False, help="Save the report to .stoner/reviews/."),
) -> None:
    """Run the deterministic AI-slop detector."""
    import time as _time

    from ..slop import run_slop
    from ..slop.report import render

    project = _project()
    targets: list[tuple[str, str]] = []  # (label rel path, text)
    if target == "all":
        for c in project.chapters():
            targets.append((project.chapter_rel(c.number), project.read(project.chapter_rel(c.number))))
        if not targets:
            _fail("No chapters found in manuscript/.")
    elif target.isdigit():
        rel = project.chapter_rel(int(target))
        targets.append((rel, project.read(rel)))
    else:
        p = Path(target)
        if not p.exists():
            _fail(f"Not found: {target}")
        targets.append((target, p.read_text(encoding="utf-8")))

    from ..canon.store import CanonStore

    bw, bp = CanonStore(project).banned_terms()
    for rel, text in targets:
        report = run_slop(text, path=rel, banned_words=bw, banned_phrases=bp)
        # rich output already carries ANSI styling; print it verbatim so
        # bracketed quotes in findings aren't parsed as rich markup.
        typer.echo(render(report, fmt))
        if save:
            out = project.resolve(
                f".stoner/reviews/slop-{Path(rel).stem}-{int(_time.time())}.json"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            console.print(f"[dim]saved {out.relative_to(project.root)}[/dim]")
        Ledger(project.root).append("slop.check", target=rel, score=report.score)


# ---------------------------------------------------------------------------
# canon
# ---------------------------------------------------------------------------


@canon_app.command("list")
def canon_list() -> None:
    """List canon entries."""
    from ..canon.store import CanonStore

    store = CanonStore(_project())
    table = Table()
    for col in ("kind", "name", "path"):
        table.add_column(col)
    for e in store.list_entries():
        table.add_row(e.kind, e.name, e.rel_path)
    console.print(table)


@canon_app.command("show")
def canon_show(path: str = typer.Argument(..., help="Canon-relative path, e.g. characters/mara.md")) -> None:
    """Print one canon entry."""
    from ..canon.store import CanonStore

    store = CanonStore(_project())
    entry = store.get(path if path.startswith("canon/") else f"canon/{path}")
    if entry is None:
        _fail(f"No canon entry at {path}")
    if entry.frontmatter:
        console.print(json.dumps(entry.frontmatter, indent=2, ensure_ascii=False))
    console.print(entry.body)


@canon_app.command("search")
def canon_search(query: str) -> None:
    """Search canon files."""
    from ..canon.store import CanonStore

    hits = CanonStore(_project()).search(query)
    if not hits:
        console.print("[dim]no matches[/dim]")
    for h in hits:
        console.print(f"[bold]{h.rel_path}[/bold]")
        for line in h.lines:
            console.print(f"  {line}")


@canon_app.command("pack")
def canon_pack(max_chars: int = typer.Option(12000)) -> None:
    """Print the canon context pack exactly as agents receive it."""
    from ..canon.store import CanonStore

    console.print(CanonStore(_project()).context_pack(max_chars=max_chars))


@canon_app.command("new")
def canon_new(
    kind: str = typer.Argument(..., help="character | world"),
    name: str = typer.Argument(..., help="Entry name, e.g. 'Mara Quill'."),
) -> None:
    """Create a character or world entry from the template."""
    from ..canon.scaffold import new_canon_entry

    project = _project()
    try:
        rel, content = new_canon_entry(kind, name)
    except ValueError as e:
        _fail(str(e))
    if (project.root / rel).exists():
        _fail(f"{rel} already exists.")
    project.write(rel, content)
    Ledger(project.root).append("canon.new", target=rel)
    hint = "voice" if kind == "character" else "rules and description"
    console.print(f"[green]Created[/green] {rel} — fill in the facts and {hint} sections.")


@chapter_app.command("new")
def chapter_new(
    number: int = typer.Argument(..., help="Chapter number."),
    title: str = typer.Option("", help="Working title."),
    pov: str = typer.Option("", help="POV character."),
) -> None:
    """Create an empty chapter stub (structure only, no AI involved)."""
    from ..canon.scaffold import new_chapter_stub

    project = _project()
    rel = project.chapter_rel(number)
    if (project.root / rel).exists():
        _fail(f"{rel} already exists. Edit it directly, or pick another number.")
    fm, body = new_chapter_stub(number, title=title, pov=pov)
    project.write_chapter(number, fm, body)
    Ledger(project.root).append("chapter.new", target=rel)
    console.print(f"[green]Created[/green] {rel}")


@chapter_app.command("import")
def chapter_import(
    number: int = typer.Argument(..., help="Chapter number to import into."),
    source: Path = typer.Argument(..., help="File containing the prose (txt/md)."),
    title: str = typer.Option("", help="Working title."),
    pov: str = typer.Option("", help="POV character."),
    status: str = typer.Option("draft", help="outline | draft | revised | final"),
) -> None:
    """Bring existing prose into the project as a chapter."""
    project = _project()
    if not source.exists():
        _fail(f"Not found: {source}")
    rel = project.chapter_rel(number)
    if (project.root / rel).exists():
        _fail(f"{rel} already exists — refusing to overwrite. Remove it first if you mean it.")
    from ..project import split_frontmatter

    fm, body = split_frontmatter(source.read_text(encoding="utf-8"))
    fm = {**fm, "status": status}
    if title:
        fm["title"] = title
    if pov:
        fm["pov"] = pov
    project.write_chapter(number, fm, body)
    Ledger(project.root).append("chapter.import", target=rel, source=str(source))
    console.print(f"[green]Imported[/green] {source} -> {rel}")


@app.command()
def beats(
    chapter: int = typer.Argument(..., help="Chapter number to create a beat sheet for."),
) -> None:
    """Create a beat sheet stub for a chapter (outline/beats/ch-NN.md)."""
    from ..canon.scaffold import new_beats_stub

    project = _project()
    rel = f"outline/beats/ch-{chapter:02d}.md"
    if (project.root / rel).exists():
        _fail(f"{rel} already exists.")
    project.write(rel, new_beats_stub(chapter))
    Ledger(project.root).append("beats.new", target=rel)
    console.print(f"[green]Created[/green] {rel} — the writer agent drafts from this.")


@app.command()
def threads() -> None:
    """List plot threads and their status."""
    from ..canon.store import CanonStore

    rows = CanonStore(_project()).threads()
    table = Table(title="plot threads")
    for col in ("id", "thread", "opened", "status", "resolved", "notes"):
        table.add_column(col)
    for t in rows:
        table.add_row(t.id, t.thread, t.opened_in, t.status, t.resolved_in, t.notes)
    console.print(table)


# ---------------------------------------------------------------------------
# write / review / revise / archive
# ---------------------------------------------------------------------------


@app.command()
def write(
    chapter: int = typer.Argument(..., help="Chapter number to draft."),
    model: str = typer.Option(
        None,
        help="Override the writer model for drafting, e.g. openai/gpt-5.2 "
        "(revise and archivist stages keep their configured roles).",
    ),
    task: str = typer.Option("", help="Extra drafting instructions."),
    skip_archive: bool = typer.Option(False, help="Skip the archivist canon-sync stage."),
) -> None:
    """Draft a chapter through the full pipeline (draft -> slop gate -> archive)."""
    from ..pipelines.write import run_write

    project = _project()
    try:
        res = run_write(project, chapter, model=model, skip_archive=skip_archive, task=task)
    except (ProviderError, RuntimeError, ValueError) as e:
        _fail(str(e))
    console.print(f"[green]ch-{chapter:02d} written[/green] — {res.words:,} words")
    console.print(
        f"slop: {res.slop_before:.1f} -> {res.slop_after:.1f} "
        f"({res.revision_loops} revision loop(s), gate {'passed' if res.gate_passed else 'FAILED'})"
    )
    if res.archive:
        console.print(
            f"archivist: {len(res.archive.applied_facts)} facts applied, "
            f"{len(res.archive.conflicts)} conflict(s)"
        )
        for c in res.archive.conflicts:
            console.print(f"  [yellow]conflict[/yellow] {c.entity}.{c.field}: canon={c.canon_value!r} new={c.new_value!r}")
    for note in res.notes:
        console.print(f"[yellow]{note}[/yellow]")


@app.command()
def review(
    chapter: int = typer.Argument(..., help="Chapter number to review."),
    passes: str = typer.Option(
        None, help="Comma-separated passes (default from stoner.yaml). "
        "Available: continuity,pacing,voice,line,logic,adversarial,panel,grade"
    ),
    model: str = typer.Option(None, help="Override reviewer model."),
) -> None:
    """Run critic passes over a chapter and save a review report."""
    from ..review.runner import run_review

    project = _project()
    pass_list = [p.strip() for p in passes.split(",")] if passes else None
    try:
        report = run_review(project, chapter, passes=pass_list, model=model)
    except (ProviderError, ValueError, ProjectError) as e:
        _fail(str(e))
    _print_findings(report.findings)
    if report.summary:
        console.print(f"\n[bold]summary:[/bold] {report.summary}")
    console.print(f"[dim]{len(report.findings)} findings saved to .stoner/reviews/[/dim]")


def _print_findings(findings: list[Finding]) -> None:
    table = Table(title="findings")
    for col in ("sev", "pass", "category", "quote", "issue"):
        table.add_column(col, overflow="fold")
    for f in findings:
        table.add_row(
            f.severity.value,
            f.source.replace("review:", ""),
            f.category,
            (f.quote[:60] + "…") if len(f.quote) > 60 else f.quote,
            f.issue,
        )
    console.print(table)


@app.command()
def revise(
    chapter: int = typer.Argument(..., help="Chapter to revise."),
    report_file: str = typer.Option(None, "--report", help="Review report JSON (default: latest for chapter)."),
    all_findings: bool = typer.Option(
        False, "--all", help="Apply all open findings, not just accepted ones."
    ),
    model: str = typer.Option(None, help="Override reviewer model."),
) -> None:
    """Apply review findings: the model rewrites the chapter."""
    project = _project()
    report = _load_review_report(project, chapter, report_file)
    if report is None:
        _fail(f"No review report found for ch-{chapter:02d}. Run: stoner review {chapter}")
    wanted = {"accepted"} if not all_findings else {"accepted", "open"}
    findings = [f for f in report.findings if f.status in wanted]
    if not findings:
        _fail(
            "No findings to apply. Accept findings in the UI (stoner ui) or pass --all."
        )
    from ..review.revise import revise_chapter

    try:
        res = revise_chapter(project, chapter, findings, model=model)
    except (ProviderError, ValueError) as e:
        _fail(str(e))
    console.print(
        f"[green]revised ch-{chapter:02d}[/green]: {res.old_words:,} -> {res.new_words:,} words, "
        f"{res.applied} finding(s) applied"
    )
    if res.summary:
        console.print(res.summary)


def _load_review_report(
    project: WritingProject, chapter: int, report_file: str | None
) -> ReviewReport | None:
    rdir = project.root / ".stoner" / "reviews"
    if report_file:
        p = Path(report_file)
        if not p.exists():
            p = rdir / report_file
        if not p.exists():
            return None
        return ReviewReport.model_validate_json(p.read_text(encoding="utf-8"))
    if not rdir.exists():
        return None
    candidates = sorted(rdir.glob(f"ch-{chapter:02d}-*.json"), reverse=True)
    if not candidates:
        return None
    return ReviewReport.model_validate_json(candidates[0].read_text(encoding="utf-8"))


@app.command()
def archive(
    chapter: int = typer.Argument(..., help="Chapter to extract facts from."),
    auto: bool = typer.Option(False, help="Apply non-conflicting updates (default: preview)."),
    model: str = typer.Option(None, help="Override archivist model."),
) -> None:
    """Sync canon and memory with what a chapter actually says."""
    from ..pipelines.write import run_archive

    project = _project()
    try:
        res = run_archive(project, chapter, model=model, auto=auto)
    except (ProviderError, ValueError, ProjectError) as e:
        _fail(str(e))
    mode = "applied" if auto else "would apply (preview — rerun with --auto)"
    console.print(f"{mode}: {len(res.applied_facts)} fact(s)")
    for f in res.applied_facts:
        console.print(f"  {f.get('kind')}: {f.get('entity')}.{f.get('field')} = {f.get('value')!r}")
    for c in res.conflicts:
        console.print(
            f"[yellow]conflict[/yellow] {c.entity}.{c.field}: canon={c.canon_value!r} new={c.new_value!r} — resolve by hand"
        )
    for e in res.new_entities:
        console.print(f"[cyan]new entity[/cyan] {e.get('kind')}: {e.get('entity') or e.get('name')}")


# ---------------------------------------------------------------------------
# ledger / providers / ui
# ---------------------------------------------------------------------------


@app.command()
def ledger(n: int = typer.Option(20, help="Entries to show.")) -> None:
    """Show recent harness actions."""
    entries = Ledger(_project().root).tail(n)
    table = Table(title="ledger")
    for col in ("when", "action", "target", "detail"):
        table.add_column(col, overflow="fold")
    import datetime

    for e in entries:
        table.add_row(
            datetime.datetime.fromtimestamp(e.ts).strftime("%m-%d %H:%M"),
            e.action,
            e.target,
            json.dumps(e.detail) if e.detail else "",
        )
    console.print(table)


@app.command()
def providers() -> None:
    """List known providers and whether their API keys are set."""
    from ..config import resolve_api_key
    from ..providers.registry import BUILTIN_PROVIDERS

    project_cfg = None
    try:
        project_cfg = WritingProject.find().config
    except ProjectError:
        pass
    table = Table(title="providers")
    for col in ("name", "kind", "base_url", "auth"):
        table.add_column(col)
    merged = dict(BUILTIN_PROVIDERS)
    if project_cfg:
        merged.update(project_cfg.providers)
    for name, pc in sorted(merged.items()):
        if pc.kind in ("codex_cli", "claude_code"):
            import shutil

            binary = "codex" if pc.kind == "codex_cli" else "claude"
            auth = (
                f"{binary} CLI found"
                if shutil.which(binary)
                else f"[red]{binary} CLI missing[/red]"
            )
        elif pc.kind == "openai_compat" and not pc.api_key_env:
            auth = "no key needed"
        else:
            key = resolve_api_key(pc, [pc.api_key_env] if pc.api_key_env else [])
            auth = "[green]key set[/green]" if key else f"[yellow]{pc.api_key_env or 'key'} unset[/yellow]"
        table.add_row(name, pc.kind, pc.base_url or "-", auth)
    console.print(table)


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8377, help="Port."),
) -> None:
    """Serve the local web dashboard."""
    project = _project()
    try:
        from ..ui.server import run_server
    except ImportError:
        _fail("UI extra not installed. Run: pip install 'st0n3r[ui]'")
    run_server(project, host=host, port=port)


from . import (  # noqa: E402
    book_cmds,
    drafts_cmds,
    foundation_cmds,
    pacing_cmds,
    room_cmds,
    voice_cmds,
)

# Feature command groups register here. New register lines append below in
# plan-number order: voice, cast, tournament, pacing, room, facts, motifs,
# readers, drafts, ship. Config fields and DIRS entries follow the same order.
book_cmds.register(app)
foundation_cmds.register(app)
voice_cmds.register(app)
pacing_cmds.register(app)
room_cmds.register(app)
drafts_cmds.register(app)


def app_main() -> None:
    # Suppress noisy tracebacks in production; STONER_DEBUG=1 re-enables.
    if not os.environ.get("STONER_DEBUG"):
        import sys

        sys.tracebacklimit = 0
    app()


if __name__ == "__main__":
    app_main()
