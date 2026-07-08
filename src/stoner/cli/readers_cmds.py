"""CLI commands for reader simulation: the `stoner readers` group.

Registered onto the main Typer app by `register(app)` (called from
`cli/main.py`), mirroring `book_cmds.py`/`cast_cmds.py`. Heavy imports live
inside command bodies so `stoner --help` stays fast and network-free, and so
tests can monkeypatch the pipeline entry points. Mutating commands rely on the
pipeline layer for ledgering.
"""

from __future__ import annotations

from pathlib import Path
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


def _parse_chapters(spec: str | None) -> list[int] | None:
    """Parse a `--chapters` spec like `1,3,5` or `1-4` or `1-3,7` into ints."""
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out or None


def _progress_printer() -> Any:
    def on_event(event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "chapter.done":
            console.print(f"[green]ch-{event['n']:02d} read[/green] — {event['markers']} marker(s)")
        elif etype == "batch.miss":
            console.print(f"[yellow]ch-{event['n']:02d} batch missed: {', '.join(event['personas'])}[/yellow]")
        elif etype == "budget.cap":
            console.print(f"[yellow]call budget reached at ch-{event['n']:02d} — resume to continue[/yellow]")
        elif etype == "bench.chapter":
            console.print(
                f"[green]ch-{event['n']:02d} benched[/green] — manuscript {event['manuscript']} / comp {event['comp']}"
            )

    return on_event


def register(app: typer.Typer) -> None:
    """Attach the `readers` command group (with nested `comps`) to `app`."""
    readers_app = typer.Typer(
        help="Reader simulation at scale: personas read the manuscript and vote.",
        no_args_is_help=True,
    )
    app.add_typer(readers_app, name="readers")
    comps_app = typer.Typer(help="Manage public-domain comps for benchmarking.", no_args_is_help=True)
    readers_app.add_typer(comps_app, name="comps")

    @readers_app.command("personas")
    def personas() -> None:
        """List available reader personas (shipped + project), marking the roster."""
        from ..readers.personas import default_roster, load_personas, select_roster

        project = _project()
        people = load_personas(project)
        cfg = project.config.readers
        try:
            roster = set(select_roster(people, cfg.roster, cfg.roster_size))
        except Exception:  # noqa: BLE001 - a bad explicit roster shouldn't break listing
            roster = set(default_roster(people, cfg.roster_size))
        table = Table(title=f"personas ({len(people)})")
        for col in ("id", "name", "age", "patience", "genre", "roster"):
            table.add_column(col)
        for p in sorted(people.values(), key=lambda x: x.id):
            table.add_row(
                p.id, p.name, str(p.age), p.patience, p.primary_genre, "●" if p.id in roster else ""
            )
        console.print(table)

    @readers_app.command("run")
    def run(
        chapters: str = typer.Option(None, "--chapters", help="Chapters to read, e.g. 1-5 or 1,3,7."),
        roster_size: int = typer.Option(None, "--roster-size", help="Override the roster size."),
        resume: bool = typer.Option(False, "--resume", help="Resume the latest reader run."),
        max_calls: int = typer.Option(None, "--max-calls", help="Cap model calls this run."),
        model: str = typer.Option(None, "--model", help="Override the reader model."),
    ) -> None:
        """Simulate the roster reading the manuscript, then build the heatmap."""
        from ..readers.heatmap import run_heatmap
        from ..readers.simulate import run_readers

        project = _project()
        if roster_size is not None:
            project.config.readers.roster_size = roster_size
        try:
            result = run_readers(
                project,
                chapters=_parse_chapters(chapters),
                model=model,
                max_calls=max_calls,
                resume=resume,
                on_event=_progress_printer(),
            )
        except (ProviderError, ValueError, ProjectError) as e:
            _fail(str(e))

        table = Table(title="readers run")
        table.add_column("metric")
        table.add_column("value", justify="right")
        table.add_row("run id", result.run_id)
        table.add_row("chapters read", str(result.chapters_read))
        table.add_row("model calls", str(result.calls))
        table.add_row("markers", str(result.markers))
        table.add_row("misses", str(result.misses))
        if result.stopped_at_cap:
            table.add_row("stopped", "at call cap (resume to continue)")
        console.print(table)

        try:
            report, _paths = run_heatmap(project, result.run_id)
        except (ValueError, ProjectError) as e:
            _fail(str(e))
        console.print(
            f"[dim]heatmap: {len(report.segments)} segments, "
            f"{len(report.findings)} trouble segment(s) — {result.run_id}[/dim]"
        )
        for note in result.notes[:5]:
            console.print(f"[yellow]{note}[/yellow]")

    @readers_app.command("heatmap")
    def heatmap(
        run_id: str = typer.Argument(None, help="Run id (default: the latest reader run)."),
    ) -> None:
        """Render a reader run's attention heatmap and trouble segments."""
        from ..readers.heatmap import run_heatmap
        from ..readers.state import list_runs

        project = _project()
        if run_id is None:
            runs = [s for s in list_runs(project) if s.kind == "readers"]
            if not runs:
                _fail("No reader runs found. Run `stoner readers run` first.")
            run_id = runs[0].run_id
        try:
            report, _paths = run_heatmap(project, run_id)
        except (ValueError, ProjectError) as e:
            _fail(str(e))

        console.print(f"[bold]heatmap[/bold] — run {run_id}  ({report.roster_size} readers)")
        by_chapter: dict[int, list] = {}
        for s in report.segments:
            by_chapter.setdefault(s.chapter, []).append(s)
        table = Table(title="attention by chapter")
        for col in ("ch", "segments", "min attention", "trouble"):
            table.add_column(col)
        for ch in sorted(by_chapter):
            segs = by_chapter[ch]
            worst = min((s.attention for s in segs), default=0.0)
            trouble = sum(1 for f in report.findings if f.category.startswith(f"ch-{ch:02d}"))
            table.add_row(f"{ch:02d}", str(len(segs)), f"{worst:+.2f}", str(trouble))
        console.print(table)
        for f in report.findings[:10]:
            console.print(f"  [yellow]{f.category}[/yellow] {f.issue}")
        for note in report.notes:
            console.print(f"[dim]{note}[/dim]")

    @readers_app.command("bench")
    def bench(
        comp: str = typer.Argument(..., help="Comp slug to benchmark against."),
        chapters: str = typer.Option(None, "--chapters", help="Chapters to bench, e.g. 1-5."),
        roster_size: int = typer.Option(None, "--roster-size", help="Override the roster size."),
        resume: bool = typer.Option(False, "--resume", help="Resume the latest bench run for this comp."),
        max_calls: int = typer.Option(None, "--max-calls", help="Cap model calls this run."),
        model: str = typer.Option(None, "--model", help="Override the reader model."),
    ) -> None:
        """Blind pairwise-bench the manuscript against a comp, chapter-aligned."""
        from ..readers.bench import run_bench

        project = _project()
        if roster_size is not None:
            project.config.readers.roster_size = roster_size
        try:
            result = run_bench(
                project,
                comp,
                chapters=_parse_chapters(chapters),
                model=model,
                max_calls=max_calls,
                resume=resume,
                on_event=_progress_printer(),
            )
        except (ProviderError, ValueError, ProjectError) as e:
            _fail(str(e))

        table = Table(title=f"bench vs {comp}")
        for col in ("ch", "manuscript", "comp", "win rate"):
            table.add_column(col)
        for row in result.per_chapter:
            table.add_row(f"{row['chapter']:02d}", str(row["manuscript"]), str(row["comp"]), f"{row['win_rate']:.0%}")
        console.print(table)
        console.print(
            f"[bold]overall manuscript win rate:[/bold] {result.manuscript_win_rate:.0%} "
            f"({result.aggregation})"
        )
        if result.ratings:
            console.print(f"[dim]elo — manuscript {result.ratings['manuscript']} / comp {result.ratings['comp']}[/dim]")
        for note in result.notes[:5]:
            console.print(f"[yellow]{note}[/yellow]")

    @comps_app.command("add")
    def comps_add(
        source: Path = typer.Argument(..., help="Local public-domain text/markdown file."),
        title: str = typer.Option(..., "--title", help="Comp title."),
        author: str = typer.Option("", "--author", help="Original author."),
        year: str = typer.Option("", "--year", help="Year of first publication."),
        source_note: str = typer.Option("", "--source", help="Where the text came from (e.g. Gutenberg)."),
        slug: str = typer.Option(None, "--slug", help="Override the derived slug."),
        force: bool = typer.Option(False, "--force", help="Overwrite an existing comp."),
    ) -> None:
        """Ingest a local public-domain text as a comp (splits into chapters)."""
        from ..readers.comps import add_comp

        project = _project()
        try:
            meta = add_comp(
                project, source, title=title, author=author, year=year, source=source_note, slug=slug, force=force
            )
        except ValueError as e:
            _fail(str(e))
        console.print(
            f"[green]added comp[/green] {meta.slug} — {meta.chapters} chapter(s) under comps/{meta.slug}/"
        )

    @comps_app.command("list")
    def comps_list() -> None:
        """List ingested comps."""
        from ..readers.comps import list_comps

        comps = list_comps(_project())
        if not comps:
            console.print("[dim]no comps — add one with `stoner readers comps add`[/dim]")
            return
        table = Table(title="comps")
        for col in ("slug", "title", "author", "year", "chapters", "source"):
            table.add_column(col)
        for m in comps:
            table.add_row(m.slug, m.title, m.author or "—", m.year or "—", str(m.chapters), m.source or "—")
        console.print(table)
