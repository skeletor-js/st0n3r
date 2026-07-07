"""CLI commands for pacing instrumentation: `stoner pacing report`.

Registered onto the main Typer app by `register(app)` (called from
`cli/main.py`), kept in its own module like `book_cmds.py`. The report is
advisory only; `--no-llm` restricts the run to the deterministic
instruments (free, offline -- no provider is constructed).
"""

from __future__ import annotations

import typer
from rich.console import Console

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
    """Attach the `pacing` command group to the given Typer app."""
    pacing_app = typer.Typer(
        help="Book-level pacing instruments: scene maps, tension curve, flatlines.",
        no_args_is_help=True,
    )
    app.add_typer(pacing_app, name="pacing")

    @pacing_app.command("report")
    def report(
        llm: bool = typer.Option(
            None,
            "--llm/--no-llm",
            help="Run the per-chapter LLM judge (tension, changes-hands, beat drift). "
            "--no-llm is free and offline. Default from stoner.yaml (pacing.llm_instruments).",
        ),
        fmt: str = typer.Option("rich", "--format", help="Output format: rich | markdown | json."),
        model: str = typer.Option(None, "--model", help="Override the judge (reviewer role) model."),
        save: bool = typer.Option(
            True, "--save/--no-save", help="Save the report to .stoner/reviews/."
        ),
    ) -> None:
        """Run the pacing instruments over every chapter and print the report."""
        from ..pacing import run_pacing
        from ..pacing.report import render

        project = _project()
        try:
            rep = run_pacing(project, llm=llm, model=model, save=save)
        except (ProviderError, ValueError, ProjectError) as e:
            _fail(str(e))
        try:
            output = render(rep, fmt)
        except ValueError as e:
            _fail(str(e))
        typer.echo(output)
        if save and fmt != "json":
            console.print(f"[dim]saved {rep.json_path}[/dim]")
