"""Local web UI: FastAPI app + static single-file dashboard.

This module is imported by `stoner ui` (and by tests) even when the optional
`ui` extra is not installed, so nothing here imports `fastapi`/`uvicorn` at
module scope. `create_app()` performs the import lazily and raises a clear
`RuntimeError` if the extra is missing.

Every endpoint reads straight from disk (via `WritingProject`, `CanonStore`,
`Ledger`, and the slop detector) on each request -- there is no in-process
cache -- so edits made outside the UI (by the CLI, an editor, or the agent)
show up on the next refresh.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from ..canon.store import CanonStore
from ..ledger import Ledger
from ..project import ProjectError, WritingProject, count_words, split_frontmatter
from ..slop import run_slop
from ..tournament.state import TournamentState
from ..tournament.state import list_states as _list_tournaments
from ..tournament.state import load_state as _load_tournament_state
from ..tournament.state import state_path as _tournament_state_path
from ..tournament.state import tournaments_dir as _tournaments_dir
from ..voice.drift import run_voice
from ..voice.fingerprint import FingerprintError, load_fingerprint

if TYPE_CHECKING:  # pragma: no cover - typing only, fastapi may be absent
    from fastapi import FastAPI

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

REVIEWS_DIR = (".stoner", "reviews")
_CHAPTER_NUM_RE = re.compile(r"ch-(\d+)")

_MISSING_FASTAPI = (
    "The st0n3r web UI needs the optional 'ui' extra. Install it with:\n"
    "    pip install 'st0n3r[ui]'"
)


class _FindingStatusUpdate(BaseModel):
    """PATCH body for /api/reviews/{file}/findings/{finding_id}."""

    status: Literal["open", "accepted", "dismissed", "fixed"]


class _CommentCreate(BaseModel):
    """POST body for /api/room/comments/{chapter}."""

    quote: str = ""
    text: str


class _CommentStatusUpdate(BaseModel):
    """PATCH body for /api/room/comments/{chapter}/{comment_id}."""

    status: Literal["open", "answered", "resolved", "dismissed"]


class _TournamentVote(BaseModel):
    """POST body for /api/tournaments/{id}/votes (mirrors the finding PATCH:
    pydantic body, path-jailed id). `token` is the server-generated pair
    token from the tournament detail payload; `pick` is the blind choice."""

    token: str
    pick: Literal["a", "b"]


# ---------------------------------------------------------------------------
# Path-jail helpers (plain python, no fastapi dependency so they stay
# importable/testable even without the extra installed)
# ---------------------------------------------------------------------------


class _BadPath(Exception):
    """A request tried to escape the directory it was jailed to."""


class _NotFound(Exception):
    """A requested resource does not exist."""


def _resolve_canon_entry(project: WritingProject, canon: CanonStore, rel_path: str):
    canon_root = (project.root / "canon").resolve()
    resolved = (project.root / rel_path).resolve()
    if not resolved.is_relative_to(canon_root):
        raise _BadPath(f"path escapes canon/: {rel_path}")
    entry = canon.get(rel_path)
    if entry is None:
        raise _NotFound(f"canon entry not found: {rel_path}")
    return entry


def _reviews_dir(project: WritingProject) -> Path:
    return project.root.joinpath(*REVIEWS_DIR)


def _safe_review_path(project: WritingProject, file: str) -> Path:
    """Jail a review report filename to `.stoner/reviews/`, no traversal."""
    if not file or Path(file).name != file:
        raise _BadPath(f"invalid review filename: {file}")
    base = _reviews_dir(project)
    resolved = (base / file).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise _BadPath(f"invalid review filename: {file}")
    return resolved


def _load_review(project: WritingProject, file: str) -> dict[str, Any]:
    path = _safe_review_path(project, file)
    if not path.exists():
        raise _NotFound(f"review report not found: {file}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _NotFound(f"malformed review report: {file}") from exc
    if not isinstance(data, dict):
        raise _NotFound(f"malformed review report: {file}")
    return data


def _save_review(project: WritingProject, file: str, data: dict[str, Any]) -> None:
    path = _safe_review_path(project, file)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _room_sessions_dir(project: WritingProject) -> Path:
    return project.root / ".stoner" / "room" / "sessions"


def _safe_room_session_path(project: WritingProject, file: str) -> Path:
    """Jail a session record filename to `.stoner/room/sessions/`, no traversal
    (same shape as `_safe_review_path`)."""
    if not file or Path(file).name != file:
        raise _BadPath(f"invalid session filename: {file}")
    base = _room_sessions_dir(project)
    resolved = (base / file).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise _BadPath(f"invalid session filename: {file}")
    return resolved


def _load_room_session(project: WritingProject, file: str) -> dict[str, Any]:
    path = _safe_room_session_path(project, file)
    if not path.exists():
        raise _NotFound(f"session record not found: {file}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _NotFound(f"malformed session record: {file}") from exc
    if not isinstance(data, dict):
        raise _NotFound(f"malformed session record: {file}")
    return data


def _list_room_sessions(project: WritingProject) -> list[dict[str, Any]]:
    base = _room_sessions_dir(project)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(base.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "file": f.name,
                "id": data.get("id"),
                "scope": data.get("scope"),
                "chapter": data.get("chapter"),
                "editors": data.get("editors", []),
                "findings": len(data.get("findings", []) or []),
                "obligations_unmet": len(data.get("obligations_unmet", []) or []),
                "created_at": data.get("created_at"),
            }
        )
    out.sort(key=lambda r: r["created_at"] or 0, reverse=True)
    return out


def _list_room_notebooks(project: WritingProject) -> list[dict[str, Any]]:
    base = project.root / ".stoner" / "room" / "notebooks"
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(base.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        items = data.get("items") if isinstance(data.get("items"), list) else []
        out.append(
            {
                "editor": data.get("editor", f.stem),
                "opinion": data.get("opinion", ""),
                "items": items,
                "updated_at": data.get("updated_at"),
            }
        )
    return out


def _review_kind(data: dict[str, Any]) -> str:
    """Label a saved report for the reviews listing.

    Reports that carry an explicit `kind` field (the repo-wide report
    discriminator; e.g. pacing reports set `kind: "pacing"`) win outright;
    legacy shape-sniffing covers the older slop/review payloads.
    """
    kind = data.get("kind")
    if isinstance(kind, str) and kind:
        return kind
    return "review" if "passes" in data else "slop"


def _chapter_from_path(path: str) -> int | None:
    m = _CHAPTER_NUM_RE.search(path or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Tournaments: path-jailed state loading and blinded pair payloads
# ---------------------------------------------------------------------------

_TOURNAMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Statuses in which the UI keeps voting open -- angle names and per-pair
# judge verdicts are withheld from payloads while one of these holds.
_VOTABLE_STATUSES = ("judging", "proposed")


def _safe_tournament_state(project: WritingProject, tournament_id: str) -> TournamentState:
    """Jail a tournament id to `.stoner/tournaments/`, no traversal."""
    if not _TOURNAMENT_ID_RE.match(tournament_id) or Path(tournament_id).name != tournament_id:
        raise _BadPath(f"invalid tournament id: {tournament_id}")
    path = _tournament_state_path(project, tournament_id)
    base = _tournaments_dir(project).resolve()
    if not path.resolve().is_relative_to(base):
        raise _BadPath(f"invalid tournament id: {tournament_id}")
    if not path.exists():
        raise _NotFound(f"tournament not found: {tournament_id}")
    return _load_tournament_state(project, tournament_id)


def _pair_token(state: TournamentState, ia: int, ib: int) -> str:
    """Deterministic opaque token for one votable pair of one tournament."""
    seed = f"{state.id}|{state.created_at}|{min(ia, ib)}|{max(ia, ib)}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _pair_order(state: TournamentState, ia: int, ib: int) -> tuple[int, int]:
    """Randomized-but-deterministic presentation order for a pair: which take
    is shown as `a`. Derived from a hash so repeated requests agree (the vote
    POST recomputes the same mapping) without storing per-request state."""
    lo, hi = min(ia, ib), max(ia, ib)
    seed = f"order|{state.id}|{state.created_at}|{lo}|{hi}"
    flip = hashlib.sha256(seed.encode("utf-8")).digest()[0] % 2
    return (lo, hi) if flip == 0 else (hi, lo)


def _votable_pairs(state: TournamentState) -> list[tuple[int, int]]:
    """Judged pairs first (deduped), then any remaining combinations."""
    pairs: list[tuple[int, int]] = []
    seen: set[frozenset[int]] = set()
    for c in state.comparisons:
        key = frozenset((c.a, c.b))
        if key not in seen:
            seen.add(key)
            pairs.append((c.a, c.b))
    for i, a in enumerate(state.takes):
        for b in state.takes[i + 1 :]:
            key = frozenset((a.index, b.index))
            if key not in seen:
                seen.add(key)
                pairs.append((a.index, b.index))
    return pairs


def _tournament_summary(state: TournamentState) -> dict[str, Any]:
    return {
        "id": state.id,
        "chapter": state.chapter,
        "status": state.status,
        "takes": len(state.takes),
        "proposed_winner": state.proposed_winner,
        "created_at": state.created_at,
    }


def _tournament_detail(project: WritingProject, state: TournamentState) -> dict[str, Any]:
    """Detail payload; blind while votable (no angles, no per-pair verdicts,
    anonymized pair bodies under randomized a/b keys)."""
    from ..tournament.takes import read_take_body

    votable = state.status in _VOTABLE_STATUSES
    takes = []
    for t in state.takes:
        row: dict[str, Any] = {
            "index": t.index,
            "words": t.words,
            "slop": t.slop,
            "rating": state.ratings.get(t.index),
            "steals": len(t.steals),
        }
        if not votable:
            row["angle"] = t.angle
        takes.append(row)

    pairs = []
    if votable:
        voted = {frozenset((v.get("take_a"), v.get("take_b"))) for v in state.votes}
        for ia, ib in _votable_pairs(state):
            if frozenset((ia, ib)) in voted:
                continue
            first, second = _pair_order(state, ia, ib)
            rec_first, rec_second = state.take(first), state.take(second)
            if rec_first is None or rec_second is None:
                continue
            try:
                pairs.append(
                    {
                        "token": _pair_token(state, ia, ib),
                        "a": read_take_body(project, rec_first),
                        "b": read_take_body(project, rec_second),
                    }
                )
            except ProjectError:
                continue

    detail: dict[str, Any] = {
        **_tournament_summary(state),
        "votable": votable,
        "standings": sorted(
            takes, key=lambda r: -(r["rating"] if r["rating"] is not None else 0)
        ),
        "pairs": pairs,
        "votes": len(state.votes),
        "notes": state.notes,
    }
    if not votable:
        detail["comparisons"] = [
            {"a": c.a, "b": c.b, "verdict": c.verdict} for c in state.comparisons
        ]
    return detail


def _list_reviews(project: WritingProject) -> list[dict[str, Any]]:
    base = _reviews_dir(project)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(base.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "file": f.name,
                "chapter": _chapter_from_path(str(data.get("path", ""))),
                "created_at": data.get("created_at"),
                "kind": _review_kind(data),
            }
        )
    out.sort(key=lambda r: r["created_at"] or 0, reverse=True)
    return out


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(project: WritingProject) -> FastAPI:
    """Build the FastAPI app serving `project`'s dashboard + JSON API.

    Raises `RuntimeError` (not `ImportError`) if `fastapi` is not installed,
    with actionable install instructions -- `stoner ui` catches nothing
    special, it just lets the message reach the user.
    """
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
    except ImportError as exc:
        raise RuntimeError(_MISSING_FASTAPI) from exc

    app = FastAPI(title="st0n3r", docs_url=None, redoc_url=None)
    canon = CanonStore(project)
    ledger = Ledger(project.root)

    def _http404(detail: str) -> HTTPException:
        return HTTPException(status_code=404, detail=detail)

    # -- static dashboard -------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        if not INDEX_HTML.exists():
            raise _http404("UI not built: static/index.html missing")
        return INDEX_HTML.read_text(encoding="utf-8")

    # -- project / manuscript ----------------------------------------------

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        return project.status_summary()

    @app.get("/api/chapters")
    def api_chapters() -> list[dict[str, Any]]:
        return [
            {
                "number": c.number,
                "title": c.title,
                "status": c.status,
                "pov": c.pov,
                "words": c.words,
            }
            for c in project.chapters()
        ]

    @app.get("/api/chapters/{number}")
    def api_chapter(number: int) -> dict[str, Any]:
        try:
            fm, body = project.read_chapter(number)
        except ProjectError as exc:
            raise _http404(f"chapter not found: ch-{number:02d}") from exc
        return {"frontmatter": fm, "body": body, "words": count_words(body)}

    @app.get("/api/chapters/{number}/slop")
    def api_chapter_slop(number: int) -> dict[str, Any]:
        rel = project.chapter_rel(number)
        try:
            text = project.read(rel)
        except ProjectError as exc:
            raise _http404(f"chapter not found: ch-{number:02d}") from exc
        bw, bp = CanonStore(project).banned_terms()
        report = run_slop(text, path=rel, banned_words=bw, banned_phrases=bp)
        data = report.model_dump(mode="json")

        # Findings carry spans over the *raw* file (frontmatter included) so
        # the CLI can point at the source file directly. The UI only ever
        # renders the frontmatter-stripped `body` (matching GET
        # /api/chapters/{number}), so rebase spans onto that same string.
        _fm, body = split_frontmatter(text)
        body_start = len(text) - len(body)
        for finding in data["findings"]:
            span = finding.get("span")
            if not span:
                continue
            span["start"] = max(0, span["start"] - body_start)
            span["end"] = max(0, span["end"] - body_start)
            span["line"] = body.count("\n", 0, span["start"]) + 1
        return data

    # -- voice ---------------------------------------------------------------

    @app.get("/api/voice")
    def api_voice() -> dict[str, Any]:
        """Fingerprint meta, or a 404 whose detail names `stoner voice learn`."""
        try:
            fp = load_fingerprint(project)
        except FingerprintError as exc:
            raise _http404(str(exc)) from exc
        return {
            "version": fp.version,
            "exemplars": fp.exemplars,
            "segment_count": fp.segment_count,
            "total_words": fp.total_words,
            "thin": fp.thin,
            "created_at": fp.created_at,
        }

    @app.get("/api/chapters/{number}/voice")
    def api_chapter_voice(number: int) -> dict[str, Any]:
        rel = project.chapter_rel(number)
        try:
            text = project.read(rel)
        except ProjectError as exc:
            raise _http404(f"chapter not found: ch-{number:02d}") from exc
        try:
            fp = load_fingerprint(project)
        except FingerprintError as exc:
            raise _http404(str(exc)) from exc
        report = run_voice(text, fp, path=rel, config=project.config.voice)
        data = report.model_dump(mode="json")

        # Same span rebase as api_chapter_slop: findings carry spans over the
        # raw file; the UI renders the frontmatter-stripped body.
        _fm, body = split_frontmatter(text)
        body_start = len(text) - len(body)
        for finding in data["findings"]:
            span = finding.get("span")
            if not span:
                continue
            span["start"] = max(0, span["start"] - body_start)
            span["end"] = max(0, span["end"] - body_start)
            span["line"] = body.count("\n", 0, span["start"]) + 1
        return data

    # -- canon --------------------------------------------------------------

    @app.get("/api/canon")
    def api_canon() -> list[dict[str, Any]]:
        return [{"rel_path": e.rel_path, "kind": e.kind, "name": e.name} for e in canon.list_entries()]

    @app.get("/api/canon/entry")
    def api_canon_entry(path: str) -> dict[str, Any]:
        try:
            entry = _resolve_canon_entry(project, canon, path)
        except _BadPath as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except _NotFound as exc:
            raise _http404(str(exc)) from exc
        return {"frontmatter": entry.frontmatter, "body": entry.body}

    @app.get("/api/threads")
    def api_threads() -> list[dict[str, Any]]:
        return [
            {
                "id": t.id,
                "thread": t.thread,
                "opened_in": t.opened_in,
                "status": t.status,
                "resolved_in": t.resolved_in,
                "notes": t.notes,
            }
            for t in canon.threads()
        ]

    # -- reviews --------------------------------------------------------------

    @app.get("/api/reviews")
    def api_reviews() -> list[dict[str, Any]]:
        return _list_reviews(project)

    @app.get("/api/reviews/{file}")
    def api_review_detail(file: str) -> dict[str, Any]:
        try:
            return _load_review(project, file)
        except _BadPath as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except _NotFound as exc:
            raise _http404(str(exc)) from exc

    @app.patch("/api/reviews/{file}/findings/{finding_id}")
    def api_review_finding_patch(
        file: str, finding_id: str, update: _FindingStatusUpdate
    ) -> dict[str, Any]:
        try:
            data = _load_review(project, file)
        except _BadPath as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except _NotFound as exc:
            raise _http404(str(exc)) from exc

        for finding in data.get("findings", []):
            if finding.get("id") == finding_id:
                finding["status"] = update.status
                _save_review(project, file, data)
                return finding
        raise _http404(f"finding not found: {finding_id}")

    # -- ledger --------------------------------------------------------------

    @app.get("/api/ledger")
    def api_ledger(n: int = 100) -> list[dict[str, Any]]:
        return [entry.model_dump(mode="json") for entry in ledger.tail(n)]

    # -- book (autonomous run state) -----------------------------------------

    @app.get("/api/book")
    def api_book() -> dict[str, Any]:
        """Contents of `.stoner/book-state.json`, or `{}` if absent/unreadable.

        The file is written by the (still in-development) `stoner book`
        autonomous run loop -- this endpoint is deliberately defensive since
        that writer's schema may still be in flux: a missing file, an empty
        file, malformed JSON, or a non-object JSON value all resolve to `{}`
        rather than a 404/500, so the dashboard always has something safe to
        render.
        """
        path = project.root / ".stoner" / "book-state.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    # -- pacing (latest saved pacing report) -----------------------------------

    @app.get("/api/pacing")
    def api_pacing() -> dict[str, Any]:
        """Newest `pacing-*.json` from `.stoner/reviews/`, or `{}` if none.

        Deliberately defensive like `/api/book`: a missing directory, no
        pacing reports, malformed JSON, or a non-object payload all resolve
        to `{}` so the Pacing panel always has something safe to render.
        """
        base = _reviews_dir(project)
        if not base.exists():
            return {}
        for f in sorted(base.glob("pacing-*.json"), reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            if isinstance(data, dict):
                return data
        return {}

    # -- tournaments (blind A/B voting; apply stays CLI-only) ------------------

    @app.get("/api/tournaments")
    def api_tournaments() -> list[dict[str, Any]]:
        return [_tournament_summary(s) for s in _list_tournaments(project)]

    @app.get("/api/tournaments/{tournament_id}")
    def api_tournament_detail(tournament_id: str) -> dict[str, Any]:
        try:
            state = _safe_tournament_state(project, tournament_id)
        except _BadPath as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except _NotFound as exc:
            raise _http404(str(exc)) from exc
        return _tournament_detail(project, state)

    @app.post("/api/tournaments/{tournament_id}/votes")
    def api_tournament_vote(tournament_id: str, vote: _TournamentVote) -> dict[str, Any]:
        """Record one blind vote. Writes `.stoner/taste.json` and the
        tournament state's vote list; never touches manuscript files."""
        from ..tournament.taste import record_vote

        try:
            state = _safe_tournament_state(project, tournament_id)
        except _BadPath as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except _NotFound as exc:
            raise _http404(str(exc)) from exc
        if state.status not in _VOTABLE_STATUSES:
            raise HTTPException(
                status_code=409, detail=f"tournament is {state.status}; voting is closed"
            )

        for ia, ib in _votable_pairs(state):
            if _pair_token(state, ia, ib) != vote.token:
                continue
            first, second = _pair_order(state, ia, ib)
            picked = first if vote.pick == "a" else second
            try:
                recorded = record_vote(project, state, (ia, ib), picked)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # The reveal: angles and the judge's verdict come back only
            # after the vote is recorded.
            judge_pick = recorded.judge_pick
            return {
                "picked": picked,
                "take_a": {"index": recorded.take_a, "angle": recorded.angle_a},
                "take_b": {"index": recorded.take_b, "angle": recorded.angle_b},
                "judge_pick": judge_pick,
            }
        raise _http404(f"no votable pair matches token {vote.token!r}")

    # -- writers' room ----------------------------------------------------------

    @app.get("/api/room/sessions")
    def api_room_sessions() -> list[dict[str, Any]]:
        return _list_room_sessions(project)

    @app.get("/api/room/sessions/{file}")
    def api_room_session_detail(file: str) -> dict[str, Any]:
        try:
            return _load_room_session(project, file)
        except _BadPath as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except _NotFound as exc:
            raise _http404(str(exc)) from exc

    @app.get("/api/room/notebooks")
    def api_room_notebooks() -> list[dict[str, Any]]:
        return _list_room_notebooks(project)

    @app.get("/api/room/comments/{chapter}")
    def api_room_comments(chapter: int) -> list[dict[str, Any]]:
        from ..room.comments import CommentStore

        return [c.model_dump(mode="json") for c in CommentStore(project).list(chapter)]

    @app.post("/api/room/comments/{chapter}")
    def api_room_comment_create(chapter: int, body: _CommentCreate) -> dict[str, Any]:
        from ..room.comments import CommentStore

        comment = CommentStore(project).add(chapter, quote=body.quote, text=body.text)
        return comment.model_dump(mode="json")

    @app.patch("/api/room/comments/{chapter}/{comment_id}")
    def api_room_comment_patch(
        chapter: int, comment_id: str, update: _CommentStatusUpdate
    ) -> dict[str, Any]:
        from ..room.comments import CommentStore

        comment = CommentStore(project).set_status(chapter, comment_id, update.status)
        if comment is None:
            raise _http404(f"comment not found: {comment_id}")
        return comment.model_dump(mode="json")

    return app


def run_server(project: WritingProject, host: str = "127.0.0.1", port: int = 8377) -> None:
    """Run the dashboard with uvicorn. Local-only by default."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(_MISSING_FASTAPI) from exc

    app = create_app(project)
    uvicorn.run(app, host=host, port=port)
