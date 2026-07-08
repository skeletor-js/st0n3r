"""CLI commands for draft tournaments: `stoner tournament run|list|status|vote|apply`.

Registered onto the main Typer app by `register(app)` (called from
`cli/main.py`), mirroring `book_cmds.py`: own consoles, `_project()`,
`_fail`, heavy imports inside command bodies, progress printing via the
orchestrator's `on_event` callback.
"""

from __future__ import annotations

import random
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from ..project import ProjectError, WritingProject
from ..providers.base import ProviderError

console = Console()
err_console = Console(stderr=True, style="bold red")

tournament_app = typer.Typer(
    help="Draft tournaments: N angled takes, blind pairwise judging, human-confirmed winner.",
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


def _load_existing_state(project: WritingProject, tournament_id: str) -> Any:
    from ..tournament.state import load_state, state_path

    if not state_path(project, tournament_id).exists():
        _fail(
            f"no tournament with id {tournament_id!r}. "
            "See `stoner tournament list` for known ids."
        )
    return load_state(project, tournament_id)


def _progress_printer() -> Any:
    def on_event(event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "take.done":
            console.print(
                f"[green]take {event['take']:02d} captured[/green] — "
                f"{event['angle']}, {event['words']:,} words"
            )
        elif etype == "take.failed":
            console.print(f"[yellow]take {event['take']:02d} ({event['angle']}) failed[/yellow]")
        elif etype == "pair.judged":
            a, b = event["pair"]
            console.print(f"  judged {a:02d} vs {b:02d}: [bold]{event['verdict']}[/bold]")
        elif etype == "winner.proposed":
            console.print(f"[bold]proposed winner: take {event['take']:02d}[/bold]")

    return on_event


def register(app: typer.Typer) -> None:
    """Attach the `tournament` command group to the given Typer app."""
    app.add_typer(tournament_app, name="tournament")


@tournament_app.command()
def run(
    chapter: int = typer.Argument(..., help="Chapter number to run the tournament on."),
    takes: int = typer.Option(None, "--takes", help="Number of takes to draft (default: config, slot-aware)."),
    max_comparisons: int = typer.Option(None, "--max-comparisons", help="Judge-call budget for this run."),
    model: str = typer.Option(None, "--model", help="Override the writer model for drafting."),
    resume: str = typer.Option(None, "--resume", help="Resume a crashed/interrupted tournament by id."),
    force: bool = typer.Option(False, "--force", help="Run over a revised/final chapter."),
) -> None:
    """Draft N takes from distinct angles, judge them blind, propose a winner."""
    from ..tournament.run import run_tournament

    project = _project()
    try:
        res = run_tournament(
            project,
            chapter,
            takes=takes,
            model=model,
            resume_id=resume,
            force=force,
            max_comparisons=max_comparisons,
            on_event=_progress_printer(),
        )
    except (ProviderError, ValueError, RuntimeError, ProjectError) as e:
        _fail(str(e))

    console.print(
        f"tournament [bold]{res.id}[/bold]: {res.takes} takes, "
        f"{res.comparisons} comparisons, status {res.status}"
    )
    if res.proposed_winner is not None:
        console.print(
            f"[bold]proposed winner: take {res.proposed_winner:02d}[/bold] — "
            f"confirm with `stoner tournament apply {res.id}`"
        )
    for note in res.notes:
        console.print(f"[yellow]note:[/yellow] {note}")
    console.print(f"[dim]state: {res.state_path}[/dim]")


@tournament_app.command(name="list")
def list_cmd() -> None:
    """List tournaments in this project."""
    from ..tournament.state import list_states

    project = _project()
    states = list_states(project)
    if not states:
        console.print("[dim]no tournaments yet — run `stoner tournament run <chapter>`[/dim]")
        return
    table = Table(title="tournaments")
    for col in ("id", "chapter", "status", "takes", "proposed"):
        table.add_column(col)
    for s in states:
        table.add_row(
            s.id,
            f"{s.chapter:02d}",
            s.status,
            str(len(s.takes)),
            f"take {s.proposed_winner:02d}" if s.proposed_winner is not None else "—",
        )
    console.print(table)


@tournament_app.command()
def status(
    tournament_id: str = typer.Argument(..., metavar="ID", help="Tournament id (see `tournament list`)."),
) -> None:
    """Standings, steals, and budget for one tournament."""
    project = _project()
    state = _load_existing_state(project, tournament_id)

    console.print(
        f"[bold]{state.id}[/bold] — chapter {state.chapter:02d}, status [bold]{state.status}[/bold]"
    )
    table = Table(title="standings")
    for col in ("take", "angle", "elo", "words", "slop", "steals banked"):
        table.add_column(col)
    ranked = sorted(state.takes, key=lambda t: -state.ratings.get(t.index, 0))
    for t in ranked:
        marker = " ←" if t.index == state.proposed_winner else ""
        table.add_row(
            f"{t.index:02d}{marker}",
            t.angle,
            f"{state.ratings.get(t.index, 0):.0f}",
            f"{t.words:,}",
            f"{t.slop:.1f}",
            str(len(t.steals)),
        )
    console.print(table)
    console.print(
        f"budget: {state.comparisons_done}/{state.max_comparisons} judge calls, "
        f"{state.tokens_used:,} tokens"
    )
    for note in state.notes:
        console.print(f"[yellow]note:[/yellow] {note}")


@tournament_app.command()
def vote(
    tournament_id: str = typer.Argument(..., metavar="ID", help="Tournament id (see `tournament list`)."),
    pairs: int = typer.Option(3, "--pairs", help="How many blind pairs to vote on."),
) -> None:
    """Blind A/B voting: read two takes, pick one, then see angles/verdict."""
    from ..tournament.state import Comparison
    from ..tournament.takes import read_take_body
    from ..tournament.taste import record_vote

    project = _project()
    state = _load_existing_state(project, tournament_id)
    if len(state.takes) < 2:
        _fail(f"tournament {tournament_id} has fewer than 2 takes; nothing to vote on.")

    # Judged pairs first (they carry a verdict to compare against), then any
    # remaining combinations; presentation order is randomized per pair.
    judged = [(c.a, c.b) for c in state.comparisons]
    seen = {frozenset(p) for p in judged}
    extra = [
        (a.index, b.index)
        for i, a in enumerate(state.takes)
        for b in state.takes[i + 1 :]
        if frozenset((a.index, b.index)) not in seen
    ]
    candidates = (judged + extra)[: max(1, pairs)]
    rng = random.Random(f"{state.id}:{len(state.votes)}")

    for ia, ib in candidates:
        first, second = (ia, ib) if rng.random() < 0.5 else (ib, ia)
        rec_first, rec_second = state.take(first), state.take(second)
        assert rec_first is not None and rec_second is not None
        console.rule("Take A")
        console.print(read_take_body(project, rec_first))
        console.rule("Take B")
        console.print(read_take_body(project, rec_second))
        pick = typer.prompt("Your pick — a, b, or s to skip").strip().lower()
        if pick not in ("a", "b"):
            console.print("[dim]skipped[/dim]")
            continue
        picked = first if pick == "a" else second
        picked_rec = rec_first if pick == "a" else rec_second
        try:
            record_vote(project, state, (ia, ib), picked)
        except ValueError as e:
            _fail(str(e))
        # Reveal only AFTER the vote is recorded (blindness invariant).
        console.print(
            f"you picked take {picked:02d} ({picked_rec.angle}); "
            f"A was take {first:02d} ({rec_first.angle}), "
            f"B was take {second:02d} ({rec_second.angle})"
        )
        judged_c: Comparison | None = next(
            (c for c in state.comparisons if frozenset((c.a, c.b)) == frozenset((ia, ib))),
            None,
        )
        if judged_c is not None:
            jw = judged_c.a if judged_c.verdict == "a" else judged_c.b if judged_c.verdict == "b" else None
            verdict_text = f"take {jw:02d}" if jw is not None else "draw"
            console.print(f"judge verdict on this pair: {verdict_text}")
    console.print("[dim]votes recorded to .stoner/taste.json[/dim]")


@tournament_app.command()
def apply(
    tournament_id: str = typer.Argument(..., metavar="ID", help="Tournament id (see `tournament list`)."),
    take: int = typer.Option(None, "--take", help="Apply this take instead of the proposed winner."),
    graft: bool = typer.Option(True, "--graft/--no-graft", help="Fold losing takes' steals into the winner."),
    force: bool = typer.Option(False, "--force", help="Apply even if the chapter changed since the run."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Write the confirmed winner into the manuscript (the human step)."""
    from ..tournament.run import apply_winner

    project = _project()
    state = _load_existing_state(project, tournament_id)
    winner_idx = take if take is not None else state.proposed_winner
    rec = state.take(winner_idx) if winner_idx is not None else None
    if rec is None:
        _fail(
            f"tournament {tournament_id} has no take {winner_idx!r} to apply; "
            "run the tournament to completion first."
        )
    if not yes:
        confirmed = typer.confirm(
            f"Apply take {rec.index:02d} ({rec.angle}, {rec.words:,} words) "
            f"to chapter {state.chapter:02d}?"
        )
        if not confirmed:
            raise typer.Exit(0)
    try:
        res = apply_winner(project, tournament_id, take=take, graft=graft, force=force)
    except (ProviderError, ValueError, RuntimeError, ProjectError) as e:
        _fail(str(e))

    console.print(
        f"[green]applied take {res.take:02d}[/green] to chapter {res.chapter:02d} "
        f"({res.words:,} words{', grafted' if res.grafted else ''})"
    )
    for note in res.notes:
        console.print(f"[yellow]note:[/yellow] {note}")
