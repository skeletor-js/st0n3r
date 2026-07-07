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

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from ..canon.store import CanonStore
from ..ledger import Ledger
from ..project import ProjectError, WritingProject, count_words, split_frontmatter
from ..slop import run_slop

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

    return app


def run_server(project: WritingProject, host: str = "127.0.0.1", port: int = 8377) -> None:
    """Run the dashboard with uvicorn. Local-only by default."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(_MISSING_FASTAPI) from exc

    app = create_app(project)
    uvicorn.run(app, host=host, port=port)
