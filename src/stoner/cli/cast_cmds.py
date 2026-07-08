"""CLI commands for Character Interiority Agents: the `stoner cast` group.

Registered onto the main Typer app by `register(app)` (called from
`cli/main.py`), mirroring `book_cmds.py`. Kept in its own module so the cast
subsystem's plumbing stays out of the core command file. Heavy imports live
inside command bodies so `stoner --help` stays fast and network-free.

The privacy boundary holds here too: `cast show` is the human's window into
private state; no command exposes it to the writer agent.
"""

from __future__ import annotations

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


def register(app: typer.Typer) -> None:
    """Attach the `cast` command group to the given Typer app."""
    cast_app = typer.Typer(
        help="Character interiority: private per-character state the writer can't see.",
        no_args_is_help=True,
    )
    app.add_typer(cast_app, name="cast")

    @cast_app.command("init")
    def cast_init(
        name: str = typer.Argument(..., help="Canon character name, e.g. 'Ruth Vann'."),
    ) -> None:
        """Create a cast sheet seeded from an existing canon character (no API key)."""
        from ..interiority import CastError, CastStore
        from ..ledger import Ledger

        project = _project()
        store = CastStore(project)
        try:
            sheet = store.init_from_canon(name)
        except CastError as e:
            _fail(str(e))
        Ledger(project.root).append("cast.init", target=f".stoner/cast/{sheet.slug}.json")
        console.print(f"[green]Created cast sheet[/green] .stoner/cast/{sheet.slug}.json")
        console.print(
            "  Fill in wants (stated vs. real), fears, lies, and refusals — the "
            "seed_notes field holds the canon Wants/Fears text to structure."
        )

    @cast_app.command("list")
    def cast_list() -> None:
        """List cast sheets and a summary of each character's private state."""
        from ..interiority import CastStore

        store = CastStore(_project())
        sheets = store.list_sheets()
        if not sheets:
            console.print("[dim]no cast sheets — create one with `stoner cast init <name>`[/dim]")
            return
        table = Table(title="cast")
        for col in ("slug", "name", "knowledge", "lies", "refusals", "last ch"):
            table.add_column(col)
        for s in sheets:
            active_lies = sum(1 for lie in s.lies if lie.active)
            table.add_row(
                s.slug,
                s.name,
                str(len(s.knowledge)),
                f"{active_lies}/{len(s.lies)}",
                str(len(s.refusals)),
                str(s.last_updated_chapter),
            )
        console.print(table)

    @cast_app.command("show")
    def cast_show(slug: str = typer.Argument(..., help="Character slug, e.g. ruth-vann.")) -> None:
        """Print one character's full private state, including the knowledge ledger."""
        from ..interiority import CastError, CastStore

        store = CastStore(_project())
        try:
            sheet = store.load(slug)
        except CastError as e:
            _fail(str(e))
        console.print(f"[bold]{sheet.name}[/bold] ({sheet.slug}) — canon: {sheet.canon_ref or '—'}")
        console.print(f"  want (stated): {sheet.wants.stated or '—'}")
        console.print(f"  want (real):   {sheet.wants.real or '—'}")
        if sheet.fears:
            console.print(f"  fears: {', '.join(sheet.fears)}")
        if sheet.refusals:
            console.print("  refuses:")
            for r in sheet.refusals:
                console.print(f"    - {r.topic}" + (f" ({r.reason})" if r.reason else ""))
        if sheet.lies:
            console.print("  lies:")
            for lie in sheet.lies:
                state = "active" if lie.active else f"exposed ch-{lie.exposed_in:02d}"
                console.print(f"    - ({lie.id}) \"{lie.claim}\" to {lie.audience} [{state}]")
        table = Table(title="knowledge ledger")
        for col in ("id", "learned", "how", "secret", "fact"):
            table.add_column(col, overflow="fold")
        for k in sheet.knowledge:
            when = "backstory" if k.learned_in == 0 else f"ch-{k.learned_in:02d}"
            table.add_row(k.id, when, k.how, "yes" if k.secret else "", k.fact)
        console.print(table)

    @cast_app.command("update")
    def cast_update(
        chapter: int = typer.Argument(..., help="Chapter number to curate."),
        auto: bool = typer.Option(False, "--auto/--dry-run", help="Apply updates (default: preview)."),
        model: str = typer.Option(None, help="Override the archivist model."),
    ) -> None:
        """Extract private-state updates from a chapter, diff each sheet, apply."""
        from ..interiority import CuratorError, run_cast_update

        project = _project()
        try:
            res = run_cast_update(project, chapter, model=model, auto=auto)
        except (ProviderError, CuratorError, ProjectError, ValueError) as e:
            _fail(str(e))
        mode = "applied" if auto else "would apply (preview — rerun with --auto)"
        console.print(f"{mode}: {len(res.applied)} update(s), {len(res.conflicts)} conflict(s)")
        for a in res.applied:
            detail = a.get("fact") or a.get("claim") or a.get("topic") or a.get("kind")
            console.print(f"  {a['slug']} {a['kind']}: {detail}")
        for c in res.conflicts:
            console.print(f"  [yellow]conflict[/yellow] {c.slug} {c.kind}: {c.detail} — resolve by hand")
        for s in res.skipped:
            console.print(f"  [dim]skipped {s.get('slug', '')}: {s['reason']}[/dim]")

    @cast_app.command("check")
    def cast_check(
        chapter: int = typer.Argument(..., help="Chapter number to check."),
        model: str = typer.Option(None, help="Override the archivist model."),
    ) -> None:
        """Check a chapter for knowledge-boundedness violations (advisory, never gates)."""
        from ..interiority import BoundednessError, run_cast_check

        project = _project()
        try:
            report = run_cast_check(project, chapter, model=model)
        except (ProviderError, BoundednessError, ProjectError, ValueError) as e:
            _fail(str(e))
        if not report.findings:
            console.print("[dim]no findings[/dim]")
        else:
            table = Table(title="boundedness findings")
            for col in ("sev", "category", "quote", "issue"):
                table.add_column(col, overflow="fold")
            for f in report.findings:
                quote = (f.quote[:50] + "…") if len(f.quote) > 50 else f.quote
                table.add_row(f.severity.value, f.category, quote, f.issue)
            console.print(table)
        console.print(f"[dim]saved to .stoner/reviews/ — {report.summary}[/dim]")

    @cast_app.command("scene")
    def cast_scene(
        who: str = typer.Option(..., "--who", help="Comma-separated character slugs, e.g. ruth-vann,dale-kestner."),
        chapter: int = typer.Option(..., "--chapter", help="Chapter number the scene sits at (bounds knowledge)."),
        brief: str = typer.Option(..., "--brief", help="What the scene is about."),
        mode: str = typer.Option(None, "--mode", help="auto | multi | single (default: config)."),
        model: str = typer.Option(None, help="Override the writer model."),
    ) -> None:
        """Simulate a scene: character agents collide, then assemble a dialogue script."""
        from ..interiority import SceneError, run_scene

        project = _project()
        slugs = [s.strip() for s in who.split(",") if s.strip()]
        try:
            res = run_scene(project, slugs, chapter, brief, model=model, mode=mode)
        except (ProviderError, SceneError, ProjectError, ValueError) as e:
            _fail(str(e))
        console.print(res.script)
        console.print(
            f"\n[dim]mode: {res.mode}, {len(res.turns)} turn(s), stopped: "
            f"{res.stopped_reason} — transcript: {res.transcript_path}[/dim]"
        )
