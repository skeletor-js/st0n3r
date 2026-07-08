"""CLI commands for the Writers' Room: `stoner room ...`.

Registered onto the main Typer app by `register(app)` (called from
`cli/main.py`), mirroring `cli/book_cmds.py`: own consoles, `_project()`,
`_fail()`, heavy imports inside command bodies. Room output never gates:
`room session` exits 0 even with unmet comment obligations (a warning is
printed) -- invariant 2, the room is advisory.

Cost model (R17): a session costs one call per editor per assigned pass +
one cross-examination call per editor + at most one re-locate fallback +
at most one comment follow-up. The roster in stoner.yaml (`room.editors`)
is the cost knob.
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

room_app = typer.Typer(
    help=(
        "The Writers' Room: persistent editors with notebooks, cross-examination, "
        "and margin comments. Advisory only -- never gates. A session costs one "
        "call per editor per pass, plus one cross-exam per editor, plus at most "
        "one re-locate fallback and one comment follow-up."
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
    """Attach the `room` command group to the given Typer app."""
    app.add_typer(room_app, name="room")


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


@room_app.command("session")
def session(
    chapter: int = typer.Argument(None, help="Chapter number (omit with --book)."),
    book: bool = typer.Option(False, "--book", help="Whole-book milestone session."),
    model: str = typer.Option(None, "--model", help="Override the room model (default: room.model or reviewer role)."),
) -> None:
    """Run a room session: re-locate prior flags, takes, cross-exam, comments."""
    from ..room.session import run_room_session

    project = _project()
    if not book and chapter is None:
        _fail("Give a chapter number, or --book for a whole-book session.")
    try:
        res = run_room_session(project, chapter=chapter, book=book, model=model)
    except (ProviderError, ValueError, ProjectError) as e:
        _fail(str(e))

    table = Table(title=f"room session {res.session_id}")
    for col in ("editor", "findings"):
        table.add_column(col)
    for slug in res.editors:
        table.add_row(slug, str(res.findings_by_editor.get(slug, 0)))
    console.print(table)
    console.print(
        f"agreements: {res.agreements}   disagreements: {res.disagreements}   "
        f"findings: {res.findings_count}"
    )
    if res.relocated:
        console.print(
            "prior flags: "
            + "  ".join(f"{k}: {v}" for k, v in sorted(res.relocated.items()))
        )
    for note in res.notes:
        console.print(f"[yellow]{note}[/yellow]")
    if res.obligations_unmet:
        # Advisory: warn loudly, never a non-zero exit (invariant 2).
        console.print(
            f"[yellow]warning: {len(res.obligations_unmet)} comment obligation(s) unmet: "
            + ", ".join(res.obligations_unmet)
            + "[/yellow]"
        )
    console.print(f"[dim]saved {res.json_path}[/dim]")


# ---------------------------------------------------------------------------
# comments
# ---------------------------------------------------------------------------


@room_app.command("comment")
def comment(
    chapter: int = typer.Argument(..., help="Chapter number to annotate."),
    quote: str = typer.Option("", "--quote", help="Verbatim quote to pin the comment to."),
    text: str = typer.Option(..., "--text", help="The comment itself."),
) -> None:
    """Pin a margin comment to a chapter span (anchored via the quote)."""
    from ..room.comments import CommentStore

    project = _project()
    c = CommentStore(project).add(chapter, quote=quote, text=text)
    console.print(f"[green]comment {c.id} added[/green] to ch-{chapter:02d}")
    if c.note:
        console.print(f"[yellow]{c.note}[/yellow]")


@room_app.command("comments")
def comments(
    chapter: int = typer.Argument(..., help="Chapter number."),
    show_all: bool = typer.Option(False, "--all", help="Include resolved/dismissed comments."),
    resolve: str = typer.Option(None, "--resolve", help="Mark a comment id resolved."),
    dismiss: str = typer.Option(None, "--dismiss", help="Mark a comment id dismissed."),
) -> None:
    """List a chapter's margin comments; resolve or dismiss by id."""
    from ..room.comments import CommentStore

    project = _project()
    store = CommentStore(project)
    if resolve or dismiss:
        cid = resolve or dismiss
        status = "resolved" if resolve else "dismissed"
        updated = store.set_status(chapter, cid, status)  # type: ignore[arg-type]
        if updated is None:
            _fail(f"No comment {cid!r} on ch-{chapter:02d}.")
        console.print(f"[green]comment {cid} {status}[/green]")
        return
    rows = store.list(chapter)
    if not show_all:
        rows = [c for c in rows if c.status in ("open", "answered")]
    if not rows:
        console.print(f"[dim]no comments on ch-{chapter:02d}[/dim]")
        return
    table = Table(title=f"comments on ch-{chapter:02d}")
    for col in ("id", "status", "quote", "comment", "responses"):
        table.add_column(col, overflow="fold")
    for c in rows:
        quote_short = (c.quote[:50] + "…") if len(c.quote) > 50 else c.quote
        table.add_row(c.id, c.status, quote_short, c.text, str(len(c.responses)))
    console.print(table)
    for c in rows:
        for r in c.responses:
            console.print(f"  [dim]{c.id} <- {r.editor}:[/dim] {r.text}")


# ---------------------------------------------------------------------------
# notebook / status
# ---------------------------------------------------------------------------


@room_app.command("notebook")
def notebook(
    editor: str = typer.Argument(..., help="Editor name or slug, e.g. 'line-editor'."),
) -> None:
    """Show one editor's notebook: running opinion plus open items."""
    from ..room.notebook import Notebook
    from ..room.roster import load_roster, slugify

    project = _project()
    roster = load_roster(project.config.room)
    wanted = slugify(editor)
    match = next((e for e in roster if e.slug == wanted), None)
    if match is None:
        _fail(
            f"No editor {editor!r} in the roster. Editors: "
            + ", ".join(e.slug for e in roster)
        )
    nb = Notebook(project, match.slug, project.config.room)
    items = nb.tracked_items()
    opinion = nb.opinion
    if not opinion and not items:
        console.print(f"[dim]{match.slug}'s notebook is empty — run a session first.[/dim]")
        return
    console.print(f"[bold]{match.name}[/bold] ({match.slug})")
    if opinion:
        console.print(f"\n{opinion}\n")
    if items:
        table = Table(title="open items")
        for col in ("ch", "status", "esc", "issue", "quote"):
            table.add_column(col, overflow="fold")
        for it in items:
            quote = str(it.get("quote", ""))
            quote = (quote[:50] + "…") if len(quote) > 50 else quote
            table.add_row(
                str(it.get("chapter", "")),
                str(it.get("status", "")),
                str(it.get("escalations", 0)),
                str(it.get("issue", "")),
                quote,
            )
        console.print(table)


@room_app.command("status")
def status() -> None:
    """Open comments and persisting flags across the whole project."""
    from ..room.comments import CommentStore
    from ..room.notebook import Notebook
    from ..room.roster import load_roster

    project = _project()
    store = CommentStore(project)
    open_comments = []
    for info in project.chapters():
        open_comments.extend(store.list(info.number, only_open=True))
    console.print(f"open comments: {len(open_comments)}")
    for c in open_comments:
        console.print(f"  [dim]ch-{c.chapter:02d}[/dim] {c.id}: {c.text}")

    roster = load_roster(project.config.room)
    any_persisting = False
    for e in roster:
        nb = Notebook(project, e.slug, project.config.room)
        persisting = [it for it in nb.tracked_items() if it.get("status") == "persisting"]
        if not persisting:
            continue
        any_persisting = True
        console.print(f"[bold]{e.slug}[/bold] — {len(persisting)} persisting flag(s):")
        for it in persisting:
            console.print(
                f"  ch {it.get('chapter')} (x{it.get('escalations', 0)}): {it.get('issue', '')}"
            )
    if not any_persisting:
        console.print("[dim]no persisting flags on the record[/dim]")
