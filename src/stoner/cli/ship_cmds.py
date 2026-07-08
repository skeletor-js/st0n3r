"""CLI commands for the Production Line: `stoner ship ...`.

Registered onto the main Typer app by `register(app)` (called last in
`cli/main.py`, after `drafts_cmds`). Mirrors the nested `canon` sub-app: a
`ship` Typer group with `check`, `epub`, `pdf`, `docx`, `blurbs`, `voices`,
`audio`, and `all`. Command bodies stay thin -- the work lives in
`stoner.ship.*`; this module is CLI plumbing (project lookup, rich output,
ledgering is done inside the ship modules that own each artifact).
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
    """Attach the `ship` command group to the given Typer app."""
    ship_app = typer.Typer(
        help="Export a finished manuscript: EPUB/PDF/DOCX, blurbs, table-read audio.",
        no_args_is_help=True,
    )
    app.add_typer(ship_app, name="ship")

    @ship_app.command("check")
    def ship_check() -> None:
        """Readiness report: blockers refuse the ship, warnings only inform."""
        from ..ledger import Ledger
        from ..ship.manifest import build_manifest

        project = _project()
        manifest = build_manifest(project)
        Ledger(project.root).append(
            "ship.check",
            target=manifest.slug,
            blockers=len(manifest.blockers),
            warnings=len(manifest.warnings),
        )

        console.print(
            f"[bold]{manifest.title}[/bold] — {len(manifest.chapters)} chapter(s), "
            f"slug [cyan]{manifest.slug}[/cyan]"
        )
        table = Table(title="readiness")
        table.add_column("severity")
        table.add_column("detail", overflow="fold")
        for b in manifest.blockers:
            table.add_row("[bold red]BLOCKER[/bold red]", b)
        for w in manifest.warnings:
            table.add_row("[yellow]warning[/yellow]", w)
        if manifest.blockers or manifest.warnings:
            console.print(table)

        if manifest.blockers:
            err_console.print(
                f"{len(manifest.blockers)} blocker(s) — fix them, or ship with "
                "--allow-incomplete on the artifact commands."
            )
            raise typer.Exit(1)
        if manifest.warnings:
            console.print(
                f"[green]ready[/green] with {len(manifest.warnings)} warning(s)."
            )
        else:
            console.print("[green]ready[/green] — no blockers, no warnings.")

    @ship_app.command("epub")
    def ship_epub(
        allow_incomplete: bool = typer.Option(
            False, "--allow-incomplete", help="Ship despite readiness blockers (recorded in the ledger)."
        ),
    ) -> None:
        """Write a dependency-free, byte-reproducible EPUB3 to export/<slug>.epub."""
        from ..ship.epub import write_epub
        from ..ship.manifest import ShipError

        project = _project()
        try:
            path, chapters = write_epub(project, allow_incomplete=allow_incomplete)
        except ShipError as e:
            _fail(str(e))
        console.print(f"[green]wrote[/green] {path.relative_to(project.root)} ({chapters} chapters)")

    @ship_app.command("pdf")
    def ship_pdf(
        allow_incomplete: bool = typer.Option(
            False, "--allow-incomplete", help="Ship despite readiness blockers (recorded in the ledger)."
        ),
    ) -> None:
        """Write a typeset trade-paperback PDF to export/<slug>.pdf (needs the export extra)."""
        from ..ship.manifest import ShipError
        from ..ship.pdf import write_pdf

        project = _project()
        try:
            path, chapters = write_pdf(project, allow_incomplete=allow_incomplete)
        except ShipError as e:
            _fail(str(e))
        console.print(f"[green]wrote[/green] {path.relative_to(project.root)} ({chapters} chapters)")

    @ship_app.command("docx")
    def ship_docx(
        allow_incomplete: bool = typer.Option(
            False, "--allow-incomplete", help="Ship despite readiness blockers (recorded in the ledger)."
        ),
    ) -> None:
        """Write a Shunn submission DOCX to export/<slug>-manuscript.docx (needs the export extra)."""
        from ..ship.manifest import ShipError
        from ..ship.shunn import write_docx

        project = _project()
        try:
            path, chapters = write_docx(project, allow_incomplete=allow_incomplete)
        except ShipError as e:
            _fail(str(e))
        console.print(f"[green]wrote[/green] {path.relative_to(project.root)} ({chapters} chapters)")

    @ship_app.command("blurbs")
    def ship_blurbs(
        only: str = typer.Option(None, "--only", help="Draft just one: synopsis | query | cover."),
        force: bool = typer.Option(False, "--force", help="Overwrite existing drafts (they are yours once written)."),
        model: str = typer.Option(None, "--model", help="Override the writer model for this run."),
    ) -> None:
        """Draft synopsis / query letter / cover brief into export/ (calls the model)."""
        from ..ship.blurbs import run_blurbs

        project = _project()
        try:
            res = run_blurbs(project, only=only, force=force, model=model)
        except (ProviderError, ValueError) as e:
            _fail(str(e))
        for rel in res.written:
            console.print(f"[green]drafted[/green] {rel}")
        for rel in res.skipped:
            console.print(f"[dim]skipped {rel} (exists — pass --force to overwrite)[/dim]")

    @ship_app.command("voices")
    def ship_voices(
        backend: str = typer.Option(None, "--backend", help="TTS backend for the voice list (default: config)."),
    ) -> None:
        """Write export/audio/voices.yaml, ranking speakers by dialogue-line count."""
        from ..ship.audio import generate_voices
        from ..ship.tts import TTSError, get_backend

        project = _project()
        backend_name = backend or project.config.ship.audio.backend
        try:
            tts = get_backend(backend_name, project.config)
            path = generate_voices(project, tts)
        except TTSError as e:
            _fail(str(e))
        console.print(f"[green]wrote[/green] {path.relative_to(project.root)}")

    @ship_app.command("audio")
    def ship_audio(
        chapter: int = typer.Argument(None, help="Chapter number (omit for the whole book)."),
        backend: str = typer.Option(None, "--backend", help="TTS backend (default: config)."),
        dialogue_only: bool = typer.Option(False, "--dialogue-only", help="Render only dialogue lines."),
        announce: bool = typer.Option(False, "--announce", help="Speak each speaker's name on change."),
        assist: bool = typer.Option(False, "--assist", help="Resolve UNKNOWN lines via the model (advisory)."),
        force: bool = typer.Option(False, "--force", help="Overwrite scripts even with manual edits."),
        allow_incomplete: bool = typer.Option(
            False, "--allow-incomplete", help="Ship despite readiness blockers (recorded in the ledger)."
        ),
    ) -> None:
        """Render a stitched table-read WAV per chapter into export/audio/."""
        from ..ship.audio import generate_voices, load_voice_map, synthesize_chapter
        from ..ship.dialogue import assist_unknowns, build_script, write_script
        from ..ship.manifest import ShipError, require_ready
        from ..ship.tts import TTSError, get_backend

        project = _project()
        backend_name = backend or project.config.ship.audio.backend
        try:
            manifest = require_ready(project, allow_incomplete)
            tts = get_backend(backend_name, project.config)
        except (ShipError, TTSError) as e:
            _fail(str(e))

        voice_map = load_voice_map(project)
        if voice_map is None:
            generate_voices(project, tts)
            voice_map = load_voice_map(project) or {"narrator": "", "characters": {}}

        chapters = [chapter] if chapter is not None else [c.number for c in manifest.chapters]
        for n in chapters:
            script = build_script(project, n)
            if assist:
                script, _usage = assist_unknowns(project, script)
            try:
                write_script(project, script, force=force)
                res = synthesize_chapter(
                    project, n, tts, voice_map,
                    dialogue_only=dialogue_only, announce=announce,
                )
            except (ShipError, TTSError) as e:
                _fail(str(e))
            console.print(
                f"[green]ch-{n:02d} audio[/green] {res.wav_path.relative_to(project.root)} — "
                f"{res.chunk_count} chunk(s), {res.synthesized} synthesized, {res.cache_hits} cached"
            )
            for note in res.notes:
                console.print(f"[dim]{note}[/dim]")

    @ship_app.command("all")
    def ship_all(
        allow_incomplete: bool = typer.Option(
            False, "--allow-incomplete", help="Ship despite readiness blockers (recorded in the ledger)."
        ),
    ) -> None:
        """Run the deterministic line: check + EPUB + PDF + DOCX, then print hints."""
        from ..ship.epub import write_epub
        from ..ship.manifest import ShipError, require_ready
        from ..ship.pdf import write_pdf
        from ..ship.shunn import write_docx

        project = _project()
        try:
            manifest = require_ready(project, allow_incomplete)
        except ShipError as e:
            _fail(str(e))

        table = Table(title="ship all")
        table.add_column("format")
        table.add_column("result", overflow="fold")
        any_failed = False
        for label, writer in (("epub", write_epub), ("pdf", write_pdf), ("docx", write_docx)):
            try:
                path, _chapters = writer(project, manifest=manifest)
                table.add_row(label, f"[green]{path.relative_to(project.root)}[/green]")
            except ShipError as e:
                any_failed = True
                first_line = str(e).splitlines()[0]
                table.add_row(label, f"[red]failed[/red] — {first_line}")
        console.print(table)
        console.print(
            "[dim]hints: `stoner ship blurbs` drafts synopsis/query/cover; "
            "`stoner ship audio` renders the table read.[/dim]"
        )
        if any_failed:
            raise typer.Exit(1)
