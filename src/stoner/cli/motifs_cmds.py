"""CLI for the Promise & Motif Ledger: `stoner promises ...` and
`stoner motifs ...`.

Two sub-apps registered onto the main Typer app by one `register(app)` line
(from `cli/main.py`), mirroring `facts_cmds.py`: own consoles, `_project()`,
`_fail()`, heavy imports inside command bodies.

Split by cost and determinism:
- `promises` (list/plant/payoff/check) and `motifs` (list/add/scan) are all
  offline -- no provider is ever constructed. `promises check` is a
  deterministic gate: it exits 1 while any promise-kind thread is still open
  (`--strict` also counts plain kind-less open threads).
- Only `motifs candidates` and `motifs rhyme` call a model (the `reviewer`
  role); `--no-judge` on either runs the deterministic half only.

Every mutating or reporting command appends a `motif.*` ledger line.
"""

from __future__ import annotations

from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table

from ..project import ProjectError, WritingProject
from ..providers.base import ProviderError

console = Console()
err_console = Console(stderr=True, style="bold red")

promises_app = typer.Typer(
    help=(
        "The promise ledger: planted mysteries, threats, wants, and images "
        "tracked from plant to payoff. `promises check` is a deterministic "
        "no-unfired-guns gate. All offline -- no API key needed."
    ),
    no_args_is_help=True,
)
motifs_app = typer.Typer(
    help=(
        "The motif registry and its scans: recurrence matrix, candidate "
        "mining, and ending-rhymes-with-opening. `scan` is offline; "
        "`candidates` and `rhyme` call a model (use --no-judge to skip)."
    ),
    no_args_is_help=True,
)


def _project() -> WritingProject:
    try:
        return WritingProject.find()
    except ProjectError as e:
        err_console.print(str(e))
        raise typer.Exit(1) from None


def _fail(msg: str) -> NoReturn:
    err_console.print(msg)
    raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    """Attach the `promises` and `motifs` command groups to the given app."""
    app.add_typer(promises_app, name="promises")
    app.add_typer(motifs_app, name="motifs")


# ===========================================================================
# promises
# ===========================================================================


@promises_app.command("list")
def promises_list() -> None:
    """List planted promises (typed threads) and their status."""
    from ..canon.store import CanonStore

    rows = CanonStore(_project()).promises()
    if not rows:
        console.print(
            "[dim]no promises planted yet — plant one with `stoner promises plant`[/dim]"
        )
        return
    table = Table(title="promises")
    for col in ("id", "kind", "thread", "planted", "status", "payoff"):
        table.add_column(col, overflow="fold")
    for t in rows:
        style = "yellow" if t.status == "open" else "green"
        table.add_row(t.id, t.kind, t.thread, t.opened_in, f"[{style}]{t.status}[/{style}]", t.resolved_in)
    console.print(table)


@promises_app.command("plant")
def promises_plant(
    id: str = typer.Argument(..., help="Stable id for the promise (thread id)."),
    thread: str = typer.Argument(..., help="What promise this plants."),
    kind: str = typer.Option(..., "--kind", help="mystery | threat | want | image."),
    opened_in: str = typer.Option("", "--opened-in", help="Chapter it was planted, e.g. ch-01."),
    notes: str = typer.Option("", "--notes", help="Optional note."),
) -> None:
    """Plant a promise: an open, kind-typed thread row."""
    from ..canon.store import CanonError, CanonStore
    from ..ledger import Ledger

    project = _project()
    store = CanonStore(project)
    try:
        row = store.plant_promise(id, thread, kind, opened_in=opened_in, notes=notes)
    except CanonError as e:
        _fail(str(e))
    Ledger(project.root).append(
        "motif.promise.plant", target="canon/threads.md", id=row.id, kind=row.kind
    )
    console.print(f"[green]planted[/green] {row.kind} promise [bold]{row.id}[/bold]")


@promises_app.command("payoff")
def promises_payoff(
    id: str = typer.Argument(..., help="Promise id to pay off."),
    resolved_in: str = typer.Argument(..., help="Chapter it pays off in, e.g. ch-20."),
    notes: str = typer.Option("", "--notes", help="Optional note."),
) -> None:
    """Pay off a promise: stamp status=resolved and the payoff chapter."""
    from ..canon.store import CanonError, CanonStore
    from ..ledger import Ledger

    project = _project()
    store = CanonStore(project)
    try:
        row = store.payoff_promise(id, resolved_in, notes=notes)
    except CanonError as e:
        _fail(str(e))
    Ledger(project.root).append(
        "motif.promise.payoff", target="canon/threads.md", id=row.id, resolved_in=row.resolved_in
    )
    console.print(f"[green]paid off[/green] promise [bold]{row.id}[/bold] in {row.resolved_in}")


@promises_app.command("check")
def promises_check(
    strict: bool = typer.Option(
        False, "--strict", help="Also fail on plain (kind-less) open threads — a full loose-ends check."
    ),
) -> None:
    """No-unfired-guns gate: exit 1 while any promise-kind row is still open."""
    from ..canon.store import CanonStore
    from ..ledger import Ledger

    project = _project()
    store = CanonStore(project)
    open_promises = [p for p in store.promises() if p.status == "open"]
    open_plain = (
        [t for t in store.threads() if not t.kind and t.status == "open"] if strict else []
    )

    Ledger(project.root).append(
        "motif.promise.check",
        target="canon/threads.md",
        open_promises=len(open_promises),
        open_plain=len(open_plain),
        strict=strict,
    )

    if not open_promises and not open_plain:
        scope = "promises or open threads" if strict else "promises"
        console.print(f"[green]no open {scope} — no unfired guns[/green]")
        return

    table = Table(title="open (unfired)")
    for col in ("id", "kind", "thread", "planted"):
        table.add_column(col, overflow="fold")
    for t in open_promises:
        table.add_row(t.id, t.kind, t.thread, t.opened_in)
    for t in open_plain:
        table.add_row(t.id, "[dim]thread[/dim]", t.thread, t.opened_in)
    console.print(table)
    total = len(open_promises) + len(open_plain)
    err_console.print(f"{total} open item(s) remain — resolve or abandon before you call the book done.")
    raise typer.Exit(1)


# ===========================================================================
# motifs
# ===========================================================================


@motifs_app.command("list")
def motifs_list() -> None:
    """List registered motifs."""
    from ..canon.store import CanonStore

    rows = CanonStore(_project()).motifs()
    if not rows:
        console.print("[dim]no motifs registered — add one with `stoner motifs add`[/dim]")
        return
    table = Table(title="motifs")
    for col in ("id", "motif", "anchors", "meaning"):
        table.add_column(col, overflow="fold")
    for m in rows:
        table.add_row(m.id, m.motif, "; ".join(m.anchor_list()), m.meaning)
    console.print(table)


@motifs_app.command("add")
def motifs_add(
    id: str = typer.Argument(..., help="Stable motif id."),
    motif: str = typer.Argument(..., help="Motif name, e.g. 'the river'."),
    anchors: str = typer.Option("", "--anchors", help="Semicolon-separated anchor phrases."),
    meaning: str = typer.Option("", "--meaning", help="Intended meaning of the motif."),
    notes: str = typer.Option("", "--notes", help="Optional note."),
) -> None:
    """Register a motif by hand -- no network, no model call."""
    from ..canon.store import CanonError, CanonStore
    from ..ledger import Ledger

    project = _project()
    store = CanonStore(project)
    try:
        row = store.add_motif(id, motif, anchors=anchors, meaning=meaning, notes=notes)
    except CanonError as e:
        _fail(str(e))
    Ledger(project.root).append("motif.register", target="canon/motifs.md", id=row.id)
    console.print(f"[green]registered[/green] motif [bold]{row.id}[/bold] ({row.motif})")


@motifs_app.command("scan")
def motifs_scan(
    fmt: str = typer.Option("rich", "--format", "--fmt", help="Output format: rich | markdown | json."),
    save: bool = typer.Option(True, "--save/--no-save", help="Save the JSON report to .stoner/reviews/."),
) -> None:
    """Deterministic recurrence matrix: every registered motif across chapters."""
    from ..canon.store import CanonStore
    from ..ledger import Ledger
    from ..motifs import render, save_scan, scan_motifs

    project = _project()
    store = CanonStore(project)
    report = scan_motifs(project, store)
    try:
        output = render(report, fmt)
    except ValueError as e:
        _fail(str(e))
    typer.echo(output)

    saved = save_scan(project, report) if save else ""
    Ledger(project.root).append(
        "motif.scan", target=saved, motifs=len(report.rows), chapters=len(report.chapters), saved=save
    )
    if saved and fmt != "json":
        console.print(f"[dim]saved {saved}[/dim]")


@motifs_app.command("candidates")
def motifs_candidates(
    min_chapters: int = typer.Option(None, "--min-chapters", help="Distinct-chapter spread threshold (default from config)."),
    cap: int = typer.Option(None, "--cap", help="Max candidates to return (default from config)."),
    judge: bool = typer.Option(True, "--judge/--no-judge", help="Run the advisory LLM triage. --no-judge is offline."),
    model: str = typer.Option(None, "--model", help="Override the reviewer model."),
) -> None:
    """Mine unregistered recurring n-grams; optionally triage them with a model."""
    from ..canon.store import CanonStore
    from ..ledger import Ledger
    from ..motifs import mine_candidates, render
    from ..motifs.judge import judge_candidates

    project = _project()
    store = CanonStore(project)
    cfg = project.config.motifs
    report = mine_candidates(
        project,
        store,
        min_chapters=min_chapters if min_chapters is not None else cfg.candidate_min_chapters,
        cap=cap if cap is not None else cfg.candidate_cap,
    )
    typer.echo(render(report, "rich"))

    findings_n = 0
    if judge:
        if not report.candidates:
            console.print("[dim]no candidates to triage[/dim]")
        else:
            try:
                findings, _usage = judge_candidates(project, report, store, model=model)
            except (ProviderError, ValueError) as e:
                _fail(str(e))
            findings_n = len(findings)
            if findings:
                ftable = Table(title="triage (advisory)")
                for col in ("verdict", "gram", "note"):
                    ftable.add_column(col, overflow="fold")
                for f in findings:
                    ftable.add_row(f.category, f.quote, f.issue)
                console.print(ftable)
            console.print("[dim]advisory only — nothing written. Register with `stoner motifs add`.[/dim]")

    Ledger(project.root).append(
        "motif.candidates",
        target="canon/motifs.md",
        candidates=len(report.candidates),
        judged=judge,
        findings=findings_n,
    )


@motifs_app.command("rhyme")
def motifs_rhyme(
    window: int = typer.Option(None, "--window", help="Chapters at each end to compare (default from config)."),
    judge: bool = typer.Option(True, "--judge/--no-judge", help="Run the advisory LLM verdict. --no-judge is offline."),
    model: str = typer.Option(None, "--model", help="Override the reviewer model."),
    save: bool = typer.Option(True, "--save/--no-save", help="Save the JSON report to .stoner/reviews/."),
) -> None:
    """Does the ending rhyme with the opening? Deterministic overlap + verdict."""
    from ..canon.store import CanonStore
    from ..ledger import Ledger
    from ..motifs import render, rhyme_overlap, save_rhyme
    from ..motifs.judge import judge_rhyme

    project = _project()
    store = CanonStore(project)
    win = window if window is not None else project.config.motifs.rhyme_window
    report = rhyme_overlap(project, store, window=win)
    typer.echo(render(report, "rich"))

    findings_n = 0
    if judge:
        try:
            findings, _usage = judge_rhyme(project, report, store, window=win, model=model)
        except (ProviderError, ValueError) as e:
            _fail(str(e))
        findings_n = len(findings)
        for f in findings:
            console.print(f"[bold]{f.category}[/bold]: {f.issue}")
        console.print("[dim]advisory only — the numbers above are the deterministic measure.[/dim]")

    saved = save_rhyme(project, report) if save else ""
    Ledger(project.root).append(
        "motif.rhyme",
        target=saved,
        jaccard=report.jaccard,
        judged=judge,
        findings=findings_n,
        saved=save,
    )
    if saved:
        console.print(f"[dim]saved {saved}[/dim]")
