"""CLI commands for the fact locker: `stoner facts ...`.

Registered onto the main Typer app by `register(app)` (called from
`cli/main.py`), mirroring `cli/room_cmds.py`: own consoles, `_project()`,
`_fail()`, heavy imports inside command bodies.

Web access is explicit opt-in (`facts.enabled` in stoner.yaml) and every
network action is ledgered under `facts.*`. `facts add` and the read-only
commands need no network; `facts research` and `facts sweep` call models.
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

facts_app = typer.Typer(
    help=(
        "The fact locker: sourced real-world detail under canon/facts/. Web "
        "research is opt-in (set facts.enabled: true) and every network action "
        "is ledgered. `facts add` and `facts list/show` need no network."
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
    """Attach the `facts` command group to the given Typer app."""
    app.add_typer(facts_app, name="facts")


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------


@facts_app.command("research")
def research(
    topic: str = typer.Argument(..., help="What to research, e.g. 'Humboldt cannabis permit fees'."),
    url: list[str] = typer.Option(
        [], "--url", help="Seed URL to fetch (repeatable). Enables fetch mode for tool-capable providers."
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Write non-conflicting facts to the locker (default is dry-run)."
    ),
    model: str = typer.Option(None, "--model", help="Override the researcher model (default: models.researcher or writer)."),
) -> None:
    """Research a topic into sourced facts. Opt-in and ledgered."""
    from ..facts.research import FactsDisabledError, run_research

    project = _project()
    try:
        res = run_research(project, topic, urls=tuple(url), apply=apply, model=model)
    except FactsDisabledError as e:
        _fail(str(e))
    except (ProviderError, ValueError, ProjectError) as e:
        _fail(str(e))

    mode = "applied" if not res.dry_run else "dry-run"
    console.print(
        f"[bold]facts research[/bold] '{res.topic}' via {res.path} path ({mode}): "
        f"{len(res.candidates)} candidate(s), {len(res.applied)} to apply, "
        f"{len(res.conflicts)} conflict(s)"
    )

    if res.applied:
        table = Table(title="facts" + ("" if res.dry_run else " (written)"))
        for col in ("name", "claim", "confidence", "source"):
            table.add_column(col, overflow="fold")
        for fr in res.applied:
            claim = (fr.claim[:80] + "…") if len(fr.claim) > 80 else fr.claim
            table.add_row(fr.name, claim, fr.confidence, fr.source_url)
        console.print(table)

    if res.conflicts:
        ctable = Table(title="conflicts (not written)")
        for col in ("slug", "field", "locker", "candidate"):
            ctable.add_column(col, overflow="fold")
        for cf in res.conflicts:
            ctable.add_row(cf.slug, cf.field, str(cf.locker_value), str(cf.new_value))
        console.print(ctable)

    for note in res.notes:
        console.print(f"[yellow]{note}[/yellow]")

    if res.dry_run and res.applied:
        console.print("[dim]dry-run: re-run with --apply to write these to canon/facts/[/dim]")


# ---------------------------------------------------------------------------
# add (offline)
# ---------------------------------------------------------------------------


@facts_app.command("add")
def add(
    name: str = typer.Option(..., "--name", help="Short stable label for the fact."),
    claim: str = typer.Option(..., "--claim", help="One checkable sentence."),
    source_url: str = typer.Option(..., "--source-url", help="Where the claim came from (required)."),
    source_title: str = typer.Option("", "--source-title", help="Title of the source."),
    confidence: str = typer.Option("medium", "--confidence", help="high | medium | low."),
    tag: list[str] = typer.Option([], "--tag", help="Topic tag (repeatable)."),
    quote: str = typer.Option("", "--quote", help="Verbatim supporting passage."),
) -> None:
    """Add one fact to the locker by hand -- no network, no model call."""
    from ..canon.store import CanonStore
    from ..facts.locker import apply_facts
    from ..ledger import Ledger

    project = _project()
    store = CanonStore(project)
    candidate = {
        "name": name,
        "claim": claim,
        "source_url": source_url,
        "source_title": source_title,
        "confidence": confidence,
        "tags": list(tag),
        "quote": quote,
    }
    res = apply_facts(store, [candidate], auto=True)
    if res.conflicts:
        cf = res.conflicts[0]
        _fail(
            f"conflict: canon/facts/{cf.slug}.md already claims '{cf.locker_value}'. "
            "Edit the file directly to reconcile -- nothing was overwritten."
        )
    if not res.applied:
        _fail("nothing added (a fact needs both --name/--claim and a --source-url).")

    fr = res.applied[0]
    Ledger(project.root).append(
        "facts.add", target=f"canon/facts/{fr.slug}.md", claim=fr.claim, source=fr.source_url
    )
    console.print(f"[green]added[/green] canon/facts/{fr.slug}.md")


# ---------------------------------------------------------------------------
# list / show (read-only, no network)
# ---------------------------------------------------------------------------


@facts_app.command("list")
def list_facts_cmd() -> None:
    """List every fact in the locker."""
    from ..canon.store import CanonStore
    from ..facts.locker import _domain, list_facts

    project = _project()
    facts = list_facts(CanonStore(project))
    if not facts:
        console.print(
            "[dim]no facts in the locker yet -- add one with `stoner facts add` "
            "or research with `stoner facts research`[/dim]"
        )
        return
    table = Table(title="fact locker")
    for col in ("slug", "claim", "conf", "status", "accessed", "source"):
        table.add_column(col, overflow="fold")
    for fr in facts:
        claim = (fr.claim[:70] + "…") if len(fr.claim) > 70 else fr.claim
        table.add_row(fr.slug, claim, fr.confidence, fr.status, fr.accessed, _domain(fr.source_url))
    console.print(table)


@facts_app.command("show")
def show(slug: str = typer.Argument(..., help="Fact slug (see `stoner facts list`).")) -> None:
    """Print one fact entry verbatim."""
    from ..canon.store import CanonStore

    project = _project()
    entry = CanonStore(project).get_fact(slug)
    if entry is None:
        _fail(f"no fact {slug!r} in the locker. Run `stoner facts list` to see all slugs.")
    console.print(project.read(f"canon/facts/{slug}.md"))


# ---------------------------------------------------------------------------
# sweep (verisimilitude review pass)
# ---------------------------------------------------------------------------


@facts_app.command("sweep")
def sweep(
    chapter: int = typer.Argument(..., help="Chapter number to sweep."),
    model: str = typer.Option(None, "--model", help="Override the reviewer model."),
) -> None:
    """Run the verisimilitude pass on a chapter: contradictions + check-this."""
    from ..ledger import Ledger
    from ..review.runner import run_review

    project = _project()
    try:
        report = run_review(project, chapter, passes=["verisimilitude"], model=model)
    except (ProviderError, ValueError, ProjectError) as e:
        _fail(str(e))

    Ledger(project.root).append(
        "facts.sweep.run",
        target=project.chapter_rel(chapter),
        chapter=chapter,
        findings=len(report.findings),
    )

    if not report.findings:
        console.print(f"[green]ch-{chapter:02d}: no verisimilitude findings[/green]")
        return
    table = Table(title=f"verisimilitude sweep — ch-{chapter:02d} (advisory)")
    for col in ("sev", "category", "quote", "issue"):
        table.add_column(col, overflow="fold")
    for f in report.findings:
        quote = (f.quote[:50] + "…") if len(f.quote) > 50 else f.quote
        table.add_row(f.severity.value, f.category, quote, f.issue)
    console.print(table)
    console.print("[dim]advisory only — findings never gate. Saved to .stoner/reviews/[/dim]")
