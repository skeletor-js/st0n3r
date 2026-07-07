"""CLI commands for draft archaeology: `stoner drafts ...`.

Registered onto the main Typer app by `register(app)` (called from
`cli/main.py`), mirroring `cli/book_cmds.py`: own consoles, `_project()`,
`_fail()`, heavy imports inside command bodies. Everything except
`refactor move-reveal`, `refactor flip-pov`, and post-refactor model
verification works offline with no API key; blame/diff/restore are fully
deterministic (no model calls anywhere).
"""

from __future__ import annotations

import datetime

import typer
from rich.console import Console
from rich.table import Table

from ..project import ProjectError, WritingProject
from ..providers.base import ProviderError

console = Console()
err_console = Console(stderr=True, style="bold red")

drafts_app = typer.Typer(
    help="Draft archaeology: snapshots, provenance, restore, structural refactors.",
    no_args_is_help=True,
)
refactor_app = typer.Typer(
    help="Structural refactors: merge, split, move-reveal, flip-pov.",
    no_args_is_help=True,
)
drafts_app.add_typer(refactor_app, name="refactor")


def _project() -> WritingProject:
    try:
        return WritingProject.find()
    except ProjectError as e:
        err_console.print(str(e))
        raise typer.Exit(1) from None


def _fail(msg: str) -> None:
    err_console.print(msg)
    raise typer.Exit(1)


def _when(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def register(app: typer.Typer) -> None:
    """Attach the `drafts` command group to the given Typer app."""
    app.add_typer(drafts_app, name="drafts")


# ---------------------------------------------------------------------------
# list / show / diff / blame
# ---------------------------------------------------------------------------


@drafts_app.command("list")
def drafts_list(
    chapter: int = typer.Argument(..., help="Chapter number."),
) -> None:
    """List a chapter's draft snapshots (newest last)."""
    from ..archaeology.snapshots import DraftStore
    from ..project import count_words, split_frontmatter

    project = _project()
    store = DraftStore(project)
    manifest = store.load_manifest(chapter)
    if not manifest.entries:
        console.print(f"[dim]no snapshots for ch-{chapter:02d} — the harness has not rewritten it yet[/dim]")
        return
    table = Table(title=f"drafts of ch-{chapter:02d}")
    for col in ("seq", "reason", "when", "words", "git", "state"):
        table.add_column(col)
    for e in manifest.entries:
        words = "-"
        state = "ok"
        if e.pruned:
            state = "pruned"
        elif not e.file:
            state = "no file (body unchanged)"
        else:
            path = store.chapter_dir(chapter) / e.file
            if path.exists():
                _fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
                words = f"{count_words(body):,}"
            else:
                state = "missing"
        table.add_row(
            str(e.seq),
            e.reason,
            _when(e.ts),
            words,
            (e.git_head or "")[:8] or "-",
            state,
        )
    console.print(table)


@drafts_app.command("show")
def drafts_show(
    chapter: int = typer.Argument(..., help="Chapter number."),
    seq: int = typer.Argument(..., help="Snapshot seq (see: stoner drafts list)."),
) -> None:
    """Print one snapshot verbatim (frontmatter + body)."""
    from ..archaeology.restore import RestoreError, load_snapshot

    project = _project()
    try:
        _entry, text = load_snapshot(project, chapter, seq)
    except RestoreError as e:
        _fail(str(e))
    typer.echo(text)


@drafts_app.command("diff")
def drafts_diff(
    chapter: int = typer.Argument(..., help="Chapter number."),
    seq_a: int = typer.Argument(..., help="Older snapshot seq."),
    seq_b: int = typer.Argument(None, help="Newer snapshot seq (default: current body)."),
) -> None:
    """Unified diff between two snapshots, or a snapshot and the current body."""
    from ..archaeology.provenance import unified_diff
    from ..archaeology.restore import RestoreError, load_snapshot
    from ..project import split_frontmatter

    project = _project()
    try:
        _e, text_a = load_snapshot(project, chapter, seq_a)
        _fm, body_a = split_frontmatter(text_a)
        if seq_b is None:
            try:
                _fm, body_b = project.read_chapter(chapter)
            except ProjectError as e:
                _fail(str(e))
            label_b = "current"
        else:
            _e, text_b = load_snapshot(project, chapter, seq_b)
            _fm, body_b = split_frontmatter(text_b)
            label_b = f"seq {seq_b}"
    except RestoreError as e:
        _fail(str(e))
    diff = unified_diff(body_a, body_b, f"ch-{chapter:02d} seq {seq_a}", f"ch-{chapter:02d} {label_b}")
    if not diff:
        console.print("[dim]no differences[/dim]")
        return
    typer.echo(diff)


@drafts_app.command("blame")
def drafts_blame(
    chapter: int = typer.Argument(..., help="Chapter number."),
    quote: str = typer.Option("", "--quote", help="Only sentences containing this text."),
) -> None:
    """Attribute each sentence of the current chapter to the rewrite event
    that introduced it (deterministic — no model calls)."""
    from ..archaeology.provenance import attribute_sentences
    from ..archaeology.snapshots import DraftStore

    project = _project()
    try:
        rows = attribute_sentences(project, chapter)
    except ProjectError as e:
        _fail(str(e))
    if quote:
        needle = " ".join(quote.split()).lower()
        rows = [r for r in rows if needle in " ".join(r.sentence.split()).lower()]
        if not rows:
            _fail(f"no sentence in ch-{chapter:02d} contains {quote!r}")
    manifest = DraftStore(project).load_manifest(chapter)
    if not manifest.entries:
        console.print(f"[dim]ch-{chapter:02d} has no rewrite history — everything is the initial state[/dim]")
    table = Table(title=f"blame ch-{chapter:02d}")
    for col in ("sentence", "event", "seq", "when", "why"):
        table.add_column(col, overflow="fold")
    for r in rows:
        event = r.event
        if r.revised:
            event = f"revised in {r.event}, originated in {r.originated_event}"
        why = "; ".join(f"{k}={v}" for k, v in r.detail.items())
        if r.session:
            why = f"session={r.session}" + (f"; {why}" if why else "")
        if r.note:
            why = f"{why}; {r.note}" if why else r.note
        sentence = (r.sentence[:70] + "…") if len(r.sentence) > 70 else r.sentence
        table.add_row(sentence, event, str(r.seq) if r.seq else "-", _when(r.ts), why)
    console.print(table)


# ---------------------------------------------------------------------------
# restore / snapshot / prune / verify
# ---------------------------------------------------------------------------


@drafts_app.command("restore")
def drafts_restore(
    chapter: int = typer.Argument(..., help="Chapter number."),
    seq: int = typer.Argument(..., help="Snapshot seq to restore from."),
    paragraph: int = typer.Option(None, "--paragraph", help="Restore only this paragraph (1-based) of the snapshot."),
    at: int = typer.Option(None, "--at", help="Replace this current paragraph (1-based) when alignment is ambiguous."),
    force: bool = typer.Option(False, "--force", help="Proceed over an un-snapshotted hand-edit (it is preserved as a human-edit snapshot first)."),
) -> None:
    """Restore a chapter (or one paragraph) from a snapshot. The pre-restore
    state is itself snapshotted, so restores are always reversible."""
    from ..archaeology.restore import RestoreError, restore_chapter, restore_paragraph

    project = _project()
    try:
        if paragraph is None:
            rel = restore_chapter(project, chapter, seq, force=force)
            console.print(f"[green]restored[/green] {rel} from snapshot seq {seq}")
        else:
            rel = restore_paragraph(project, chapter, seq, paragraph, at=at, force=force)
            console.print(
                f"[green]restored[/green] paragraph {paragraph} of snapshot seq {seq} into {rel}"
            )
    except RestoreError as e:
        _fail(str(e))


@drafts_app.command("snapshot")
def drafts_snapshot(
    chapter: int = typer.Argument(..., help="Chapter number."),
) -> None:
    """Manually snapshot the current chapter as-is (reason: manual)."""
    from ..archaeology.snapshots import manual_snapshot

    project = _project()
    try:
        entry = manual_snapshot(project, chapter)
    except ProjectError as e:
        _fail(str(e))
    console.print(f"[green]snapshotted[/green] ch-{chapter:02d} as seq {entry.seq} (manual)")


@drafts_app.command("prune")
def drafts_prune(
    chapter: int = typer.Argument(..., help="Chapter number."),
    keep: int = typer.Option(None, "--keep", help="Live snapshots to keep (default: archaeology.keep_per_chapter)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be pruned without touching anything."),
) -> None:
    """Bound snapshot storage. The original draft and human-edit snapshots
    always survive; pruned entries keep their hashes so blame still works."""
    from ..archaeology.snapshots import prune_chapter_snapshots

    project = _project()
    if keep is None:
        keep = project.config.archaeology.keep_per_chapter
    if keep is None:
        _fail(
            "No --keep given and archaeology.keep_per_chapter is not set in "
            "stoner.yaml — say how many snapshots to keep."
        )
    res = prune_chapter_snapshots(project, chapter, keep, dry_run=dry_run)
    verb = "would prune" if dry_run else "pruned"
    console.print(
        f"{verb} {len(res.pruned_seqs)} snapshot(s) of ch-{chapter:02d} "
        f"(seqs {res.pruned_seqs or '—'}), removed {len(res.removed_files)} file(s), "
        f"{res.kept} kept"
    )


@drafts_app.command("verify")
def drafts_verify(
    chapters: list[int] = typer.Argument(None, help="Chapters to also model-verify (continuity review + archivist preview)."),
    model: str = typer.Option(None, "--model", help="Override the reviewer model for model verification."),
) -> None:
    """Check project integrity (deterministic; may exit nonzero). With
    chapter arguments, also run advisory model verification on them."""
    from ..archaeology.verify import verify_refactor

    project = _project()
    affected = list(chapters or [])
    res = verify_refactor(project, affected, model=model, run_model=bool(affected))
    _print_verify(res.findings, res.notes)
    if res.integrity_failed:
        _fail("integrity check failed — fix the major findings above")
    console.print("[green]integrity ok[/green]")


def _print_verify(findings, notes) -> None:
    if findings:
        table = Table(title="verification findings")
        for col in ("sev", "source", "category", "issue"):
            table.add_column(col, overflow="fold")
        for f in findings:
            table.add_row(f.severity.value, f.source, f.category, f.issue)
        console.print(table)
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")


def _finish_refactor(res) -> None:
    """Shared refactor epilogue: print notes/findings, gate on integrity."""
    for note in res.notes:
        console.print(f"[dim]{note}[/dim]")
    _print_verify(res.findings, [])
    if res.mapping:
        moved = ", ".join(f"{k}->{v}" for k, v in sorted(res.mapping.items()))
        console.print(f"renumbered: {moved}")
    if res.report_path:
        console.print(f"[dim]verification report: {res.report_path}[/dim]")
    if res.integrity_failed:
        _fail("integrity check failed after the refactor — fix the major findings above")


# ---------------------------------------------------------------------------
# refactor sub-app
# ---------------------------------------------------------------------------


@refactor_app.command("merge")
def refactor_merge(
    a: int = typer.Argument(..., help="Chapter that receives the merged prose."),
    b: int = typer.Argument(..., help="Adjacent chapter merged into it (a+1)."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip advisory model verification (integrity checks still run)."),
) -> None:
    """Merge two adjacent chapters and renumber everything above (deterministic)."""
    from ..archaeology.refactor import RefactorError, merge_chapters

    project = _project()
    try:
        res = merge_chapters(project, a, b, verify=not no_verify)
    except (RefactorError, ProjectError, ValueError) as e:
        _fail(str(e))
    console.print(f"[green]merged[/green] ch-{b:02d} into ch-{a:02d}")
    _finish_refactor(res)


@refactor_app.command("split")
def refactor_split(
    chapter: int = typer.Argument(..., help="Chapter to split."),
    at: int = typer.Option(None, "--at", help="Paragraph (1-based) that starts the new chapter."),
    at_text: str = typer.Option(None, "--at-text", help="Exact text of the paragraph that starts the new chapter."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip advisory model verification (integrity checks still run)."),
) -> None:
    """Split a chapter in two and renumber everything above (deterministic)."""
    from ..archaeology.refactor import RefactorError, split_chapter

    project = _project()
    try:
        res = split_chapter(project, chapter, at=at, at_text=at_text, verify=not no_verify)
    except (RefactorError, ProjectError, ValueError) as e:
        _fail(str(e))
    console.print(f"[green]split[/green] ch-{chapter:02d} into ch-{chapter:02d} + ch-{chapter + 1:02d}")
    _finish_refactor(res)


@refactor_app.command("move-reveal")
def refactor_move_reveal(
    src: int = typer.Argument(..., help="Chapter the reveal currently lives in."),
    dst: int = typer.Argument(..., help="Chapter the reveal moves to."),
    quote: str = typer.Option(..., "--quote", help="The reveal, quoted verbatim from the source chapter."),
    position: str = typer.Option("early", "--position", help="Where to weave it into the target: early | late."),
    model: str = typer.Option(None, "--model", help="Override the writer model."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip advisory model verification (integrity checks still run)."),
) -> None:
    """Move a reveal between chapters (model-assisted, guarded, snapshot-first)."""
    from ..archaeology.refactor import RefactorError, move_reveal

    if position not in ("early", "late"):
        _fail("--position must be 'early' or 'late'")
    project = _project()
    try:
        res = move_reveal(
            project, src, dst, quote, position=position, model=model, verify=not no_verify
        )
    except (RefactorError, ProviderError, ProjectError, ValueError) as e:
        _fail(str(e))
    console.print(f"[green]moved reveal[/green] ch-{src:02d} -> ch-{dst:02d}")
    _finish_refactor(res)


@refactor_app.command("flip-pov")
def refactor_flip_pov(
    chapter: int = typer.Argument(..., help="Chapter to rewrite."),
    to: str = typer.Option(..., "--to", help="New POV character name."),
    model: str = typer.Option(None, "--model", help="Override the writer model."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip advisory model verification (integrity checks still run)."),
) -> None:
    """Rewrite a chapter from another character's POV (model-assisted, guarded)."""
    from ..archaeology.refactor import RefactorError, flip_pov

    project = _project()
    try:
        res = flip_pov(project, chapter, to, model=model, verify=not no_verify)
    except (RefactorError, ProviderError, ProjectError, ValueError) as e:
        _fail(str(e))
    console.print(f"[green]flipped[/green] ch-{chapter:02d} to {to}'s POV")
    _finish_refactor(res)
