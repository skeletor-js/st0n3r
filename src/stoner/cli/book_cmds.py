"""CLI commands for autonomous book mode: `stoner book` and `stoner review-book`.

Registered onto the main Typer app by `register(app)` (called from
`cli/main.py`), kept in its own module so the whole-book loop's rich progress
plumbing does not clutter the core command file.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..project import ProjectError, WritingProject
from ..providers.base import ProviderError

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


def _progress_printer() -> Any:
    def on_event(event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "chapter.done":
            console.print(
                f"[green]ch-{event['n']:02d} drafted[/green] — "
                f"{event['words']:,} words, slop {event['slop']:.1f}"
            )
        elif etype == "chapter.revised":
            console.print(
                f"  [cyan]revised ch-{event['n']:02d}[/cyan] "
                f"({event['findings']} finding(s))"
            )
        elif etype == "review.round":
            console.print(
                f"[bold]review round {event['round']}[/bold]: "
                f"{event['majors']} major finding(s), verdict {event['verdict']}"
            )
        elif etype == "budget.timeout":
            console.print(f"[yellow]time budget reached before ch-{event['n']:02d}[/yellow]")
        elif etype == "promises.open":
            if event["count"]:
                console.print(
                    f"[yellow]{event['count']} promise(s) still open — "
                    "run `stoner promises check` before you call the book done.[/yellow]"
                )

    return on_event


def register(app: typer.Typer) -> None:
    """Attach the `book` and `review-book` commands to the given Typer app."""

    @app.command()
    def book(
        max_chapters: int = typer.Option(None, "--max-chapters", help="Draft at most N chapters this run."),
        review_every: int = typer.Option(4, "--review-every", help="Run a whole-book review every N chapters."),
        max_review_rounds: int = typer.Option(2, "--max-rounds", help="Max whole-book review/revise rounds per pass."),
        max_minutes: float = typer.Option(None, "--max-minutes", help="Wall-clock cap for this run (minutes)."),
        model: str = typer.Option(None, "--model", help="Override the writer model for drafting (review/revise keep their configured roles)."),
        resume: bool = typer.Option(True, "--resume/--no-resume", help="Resume from saved book state (skip written chapters)."),
    ) -> None:
        """Write the whole book: draft every planned chapter, then review and revise."""
        from ..pipelines.book import run_book

        project = _project()
        try:
            res = run_book(
                project,
                model=model,
                max_chapters=max_chapters,
                review_every=review_every,
                max_review_rounds=max_review_rounds,
                resume=resume,
                max_minutes=max_minutes,
                on_event=_progress_printer(),
            )
        except (ProviderError, ValueError, RuntimeError, ProjectError) as e:
            _fail(str(e))

        table = Table(title="book run")
        table.add_column("metric")
        table.add_column("value", justify="right")
        table.add_row("chapters written", str(res.chapters_written))
        table.add_row("total words", f"{res.total_words:,}")
        table.add_row("review rounds", str(res.review_rounds))
        table.add_row("remaining major findings", str(res.remaining_major_findings))
        table.add_row("remaining critical findings", str(res.remaining_critical_findings))
        table.add_row("remaining open promises", str(res.remaining_open_promises))
        console.print(table)
        console.print(f"[dim]state: {res.state_path}[/dim]")

        if res.remaining_critical_findings > 0:
            err_console.print(
                f"{res.remaining_critical_findings} critical finding(s) remain — "
                "review .stoner/reviews/ and revise before export."
            )
            raise typer.Exit(1)

    @app.command(name="review-book")
    def review_book(
        model: str = typer.Option(None, "--model", help="Override the reviewer model."),
    ) -> None:
        """Run one whole-manuscript review and print findings grouped by chapter."""
        from ..review.book_review import run_book_review

        project = _project()
        try:
            report = run_book_review(project, model=model)
        except (ProviderError, ValueError, ProjectError) as e:
            _fail(str(e))

        if report.overall:
            console.print(f"[bold]overall:[/bold] {report.overall}")
        console.print(
            f"verdict: [bold]{report.verdict}[/bold]  "
            f"({report.major_count} major, {report.critical_count} critical)"
        )
        grouped = report.by_chapter()
        if not grouped:
            console.print("[dim]no chapter-tagged findings[/dim]")
        for n in sorted(grouped):
            table = Table(title=f"chapter {n}")
            for col in ("sev", "category", "quote", "issue"):
                table.add_column(col, overflow="fold")
            for f in grouped[n]:
                cat = f.category.split(":", 1)[-1]
                quote = (f.quote[:60] + "…") if len(f.quote) > 60 else f.quote
                table.add_row(f.severity.value, cat, quote, f.issue)
            console.print(table)
        console.print(f"[dim]saved to {report.json_path}[/dim]")
