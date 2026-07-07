"""CLI commands for the foundation pipeline: `stoner brainstorm`, `stoner foundation`.

This module owns no `typer.Typer` app of its own; the orchestrator wires it
onto the shared app with `foundation_cmds.register(app)` (see
`cli/main.py`, which this module does not import or modify).
"""

from __future__ import annotations

import typer
from rich.console import Console

from ..pipelines.foundation import FoundationError, run_brainstorm, run_canon_generation
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


def register(app: typer.Typer) -> None:
    """Add `brainstorm` and `foundation` commands to `app`."""

    @app.command()
    def brainstorm(
        seed: str = typer.Argument(..., help="One line to a paragraph: the story's seed idea."),
        model: str = typer.Option(None, help="Override the model (defaults to the writer role)."),
        force: bool = typer.Option(False, help="Overwrite premise.md/style.md even if hand-edited."),
    ) -> None:
        """Turn a seed idea into canon/premise.md and canon/style.md."""
        project = _project()
        try:
            res = run_brainstorm(project, seed, model=model, force=force)
        except (FoundationError, ProviderError, ValueError) as e:
            _fail(str(e))
        console.print(f"[green]Brainstormed[/green] — wrote {', '.join(res.written)}")
        console.print(f"[bold]Logline:[/bold] {res.premise.get('logline', '')}")
        console.print(f"[bold]Genre:[/bold] {res.premise.get('genre', '')}")
        themes = res.premise.get("themes") or []
        if themes:
            console.print(f"[bold]Themes:[/bold] {', '.join(str(t) for t in themes)}")
        console.print(f"[bold]Promise:[/bold] {res.premise.get('promise', '')}")
        if res.title_options:
            console.print("[bold]Title options:[/bold]")
            for t in res.title_options:
                console.print(f"  - {t}")
        console.print(
            "\n[dim]Review/edit canon/premise.md and canon/style.md, then run: "
            "stoner foundation[/dim]"
        )

    @app.command()
    def foundation(
        characters: int = typer.Option(4, help="Number of characters to generate."),
        world: int = typer.Option(3, help="Number of world entries to generate."),
        model: str = typer.Option(None, help="Override the model (defaults to the writer role)."),
        force: bool = typer.Option(False, help="Regenerate elements that already have content."),
        max_loops: int = typer.Option(2, help="Max evaluate/regenerate loops over the weakest element."),
    ) -> None:
        """Generate characters, world, threads, and outline from the premise."""
        project = _project()
        console.print("[dim]running foundation pipeline: characters -> world -> threads -> outline -> evaluate...[/dim]")
        try:
            res = run_canon_generation(
                project,
                model=model,
                characters=characters,
                world_entries=world,
                force=force,
                max_loops=max_loops,
            )
        except (FoundationError, ProviderError, ValueError) as e:
            _fail(str(e))
        console.print(f"[green]characters[/green] ({len(res.characters)}): {', '.join(res.characters)}")
        console.print(f"[green]world[/green] ({len(res.world)}): {', '.join(res.world)}")
        console.print(f"[green]threads[/green] ({len(res.threads)}): {', '.join(res.threads)}")
        console.print(f"[green]outline[/green]: {len(res.chapters)} chapter(s)")
        console.print(f"[bold]evaluate:[/bold] {res.loops} loop(s), verdict: {res.verdict}")
        for note in res.notes:
            console.print(f"[yellow]{note}[/yellow]")
