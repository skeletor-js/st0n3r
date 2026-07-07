"""CLI commands for the voice engine: `stoner voice learn | show | check`.

Registered onto the main Typer app by `register(app)` (called from
`cli/main.py`), kept in its own module so the exemplar-gathering plumbing
does not clutter the core command file. Everything here is deterministic --
no provider, no network -- so these commands are safe anywhere the slop
command is.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..project import ProjectError, WritingProject

console = Console()
err_console = Console(stderr=True, style="bold red")

#: Exemplar file extensions gathered from directories.
_EXEMPLAR_SUFFIXES = (".md", ".txt")

#: Chapter statuses eligible for --from-manuscript learning.
_MANUSCRIPT_STATUSES = ("revised", "final")


def _project() -> WritingProject:
    try:
        return WritingProject.find()
    except ProjectError as e:
        err_console.print(str(e))
        raise typer.Exit(1) from None


def _fail(msg: str) -> None:
    err_console.print(msg)
    raise typer.Exit(1)


def _files_under(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix in _EXEMPLAR_SUFFIXES)
    return [path] if path.is_file() else []


def _label(project: WritingProject, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project.root))
    except ValueError:
        return str(path)


def _gather_exemplars(
    project: WritingProject, extra: list[Path], from_manuscript: bool
) -> list[tuple[str, str]]:
    """Config exemplar paths + explicit extras + (opt-in) revised/final chapters."""
    texts: list[tuple[str, str]] = []
    candidates: list[Path] = [project.root / rel for rel in project.config.voice.exemplars]
    for arg in extra:
        candidates.append(arg if arg.exists() else project.root / arg)
    for candidate in candidates:
        for f in _files_under(candidate):
            texts.append((_label(project, f), f.read_text(encoding="utf-8")))
    if from_manuscript:
        for c in project.chapters():
            if c.status in _MANUSCRIPT_STATUSES:
                rel = project.chapter_rel(c.number)
                texts.append((rel, project.read(rel)))
    return texts


def register(app: typer.Typer) -> None:
    """Attach the `voice` sub-app to the given Typer app."""
    voice_app = typer.Typer(
        help="Learn and check the project's measured voice fingerprint.",
        no_args_is_help=True,
    )
    app.add_typer(voice_app, name="voice")

    @voice_app.command()
    def learn(
        paths: list[Path] = typer.Argument(
            None, help="Extra exemplar files or directories beyond voice.exemplars."
        ),
        from_manuscript: bool = typer.Option(
            False,
            "--from-manuscript",
            help="Also learn from this project's revised/final chapters.",
        ),
    ) -> None:
        """Build the voice fingerprint from exemplar prose (deterministic)."""
        from ..ledger import Ledger
        from ..voice.fingerprint import FingerprintError, learn_fingerprint, save_fingerprint

        project = _project()
        texts = _gather_exemplars(project, list(paths or []), from_manuscript)
        if not texts:
            if from_manuscript:
                _fail(
                    "No exemplar prose found: notes/exemplars/ (voice.exemplars) is "
                    f"empty and no chapters have status {' or '.join(_MANUSCRIPT_STATUSES)}."
                )
            _fail(
                "No exemplar prose found in notes/exemplars/ (voice.exemplars). "
                "Add files there, pass file paths, or pass --from-manuscript to "
                "learn from revised/final chapters. Never happens silently: "
                "learning from your own drafts is a deliberate act."
            )
        try:
            fp = learn_fingerprint(texts)
        except FingerprintError as e:
            _fail(str(e))
        path = save_fingerprint(project, fp)
        Ledger(project.root).append(
            "voice.learn",
            target=str(path.relative_to(project.root)),
            words=fp.total_words,
            segments=fp.segment_count,
            exemplars=len(fp.exemplars),
        )
        console.print(
            f"[green]Learned voice fingerprint[/green] from {len(fp.exemplars)} "
            f"source(s): {fp.total_words:,} words in {fp.segment_count} segment(s)."
        )
        if fp.thin:
            console.print(
                "[yellow]Corpus is thin (under 5,000 words): drift scores will be "
                "damped. Add more exemplar prose for a sharper fingerprint.[/yellow]"
            )
        console.print(f"[dim]saved {path.relative_to(project.root)}[/dim]")

    @voice_app.command()
    def show() -> None:
        """Print the fingerprint digest and per-feature stats."""
        from ..voice.fingerprint import FingerprintError, load_fingerprint, render_digest

        project = _project()
        try:
            fp = load_fingerprint(project)
        except FingerprintError as e:
            _fail(str(e))
        console.print(render_digest(fp))
        table = Table(title="fingerprint features (mean ± std over exemplar segments)")
        table.add_column("feature")
        table.add_column("mean", justify="right")
        table.add_column("std", justify="right")
        fw_count = 0
        for name in sorted(fp.features):
            if name.startswith("fw."):
                fw_count += 1
                continue
            stat = fp.features[name]
            table.add_row(name, f"{stat.mean:.3f}", f"{stat.std:.3f}")
        console.print(table)
        console.print(
            f"[dim]+ {fw_count} function-word rate features (scored as one "
            "composite delta; top rates in the digest above)[/dim]"
        )

    @voice_app.command()
    def check(
        target: str = typer.Argument(..., help="Chapter number, a file path, or 'all'."),
        fmt: str = typer.Option("rich", help="Output format: rich | markdown | json."),
        save: bool = typer.Option(False, help="Save the report to .stoner/reviews/."),
    ) -> None:
        """Score prose against the learned fingerprint (0 = in voice)."""
        import time as _time

        from ..ledger import Ledger
        from ..voice.drift import run_voice
        from ..voice.fingerprint import FingerprintError, load_fingerprint
        from ..voice.report import render

        project = _project()
        try:
            fp = load_fingerprint(project)
        except FingerprintError as e:
            _fail(str(e))
        targets: list[tuple[str, str]] = []  # (label rel path, text)
        if target == "all":
            for c in project.chapters():
                rel = project.chapter_rel(c.number)
                targets.append((rel, project.read(rel)))
            if not targets:
                _fail("No chapters found in manuscript/.")
        elif target.isdigit():
            rel = project.chapter_rel(int(target))
            try:
                targets.append((rel, project.read(rel)))
            except ProjectError as e:
                _fail(str(e))
        else:
            p = Path(target)
            if not p.exists():
                _fail(f"Not found: {target}")
            targets.append((target, p.read_text(encoding="utf-8")))

        for rel, text in targets:
            report = run_voice(text, fp, path=rel, config=project.config.voice)
            # rich output already carries ANSI styling; print it verbatim so
            # bracketed quotes in findings aren't parsed as rich markup.
            typer.echo(render(report, fmt))
            if save:
                out = project.resolve(
                    f".stoner/reviews/voice-{Path(rel).stem}-{int(_time.time())}.json"
                )
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
                console.print(f"[dim]saved {out.relative_to(project.root)}[/dim]")
            Ledger(project.root).append("voice.check", target=rel, score=report.score)
