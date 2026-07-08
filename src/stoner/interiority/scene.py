"""Scene simulation: character agents collide to produce dialogue by asymmetry.

Puts N character agents in a room. Each turn is one plain completion whose
system prompt carries ONLY that character's private sheet (knowledge bounded
to the scene's chapter) plus the public transcript -- no character ever sees
another's sheet, so information asymmetry is structural, not prompted. Turn
order is deterministic round-robin; the scene ends when every character passes
in one full round, `scene_max_rounds` is hit, or the token budget trips. The
transcript is written to disk incrementally (mirroring `engine/agent.py`) so an
interrupted sim leaves usable state.

Two modes (invariant 7). Multi-call is the default on tools-capable providers:
one completion per turn. Text-only providers (`supports_tools=False`) degrade
to a single-call role-play -- one completion carrying every character's
hidden-state block and a strict line protocol, parsed deterministically into
the same turn-log shape. `SceneResult` is mode-invariant.

The sim never writes manuscript files. It saves a prose script under
`.stoner/cast/scenes/`; the human feeds that to `stoner write N --task`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..canon.store import CanonStore
from ..ledger import Ledger
from ..pipelines.common import render_prompt, resolve_role_model
from ..project import WritingProject
from ..providers.base import Provider
from ..providers.registry import get_provider
from ..review.passes import extract_json
from ..types import CompletionRequest, Message, Usage
from .store import CastStore, private_digest


class SceneError(RuntimeError):
    """Raised for an invalid scene request or an unparseable single-call block."""


_SCENES_DIR = ".stoner/cast/scenes"
_FORMAT_CORRECTION = (
    "Your last reply was not valid JSON in the required per-turn shape. Reply "
    'with ONLY a JSON object like {"speech": "...", "action": "...", '
    '"private_note": "...", "pass": false}.'
)


class SceneTurn(BaseModel):
    """One turn in the log. `private_note` never enters the public transcript."""

    round: int
    speaker: str
    speech: str = ""
    action: str = ""
    private_note: str = ""
    passed: bool = False


@dataclass
class SceneResult:
    script: str
    transcript_path: str
    mode: str
    turns: list[SceneTurn] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stopped_reason: str = "all_passed"
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _canon_voice(project: WritingProject, slug: str) -> str:
    from ..canon.store import extract_section

    entry = CanonStore(project).get_character(slug)
    if entry is None:
        return ""
    return extract_section(entry.body, "Voice")


def _public_line(turn: SceneTurn) -> str:
    parts = []
    if turn.speech:
        parts.append(f"{turn.speaker.upper()}: {turn.speech}")
    if turn.action:
        parts.append(f"{turn.speaker.upper()} [{turn.action}]")
    return "\n".join(parts)


def _public_transcript(turns: list[SceneTurn]) -> str:
    lines = [_public_line(t) for t in turns if (t.speech or t.action)]
    return "\n".join(line for line in lines if line)


def _resolve_provider(
    project: WritingProject, model: str | None, provider: Provider | None
) -> tuple[Provider, str]:
    model_str = resolve_role_model(project.config, "writer", model)
    if provider is None:
        return get_provider(model_str, project.config)
    model_id = model_str.split("/", 1)[1] if "/" in model_str else model_str
    return provider, model_id


def _complete(provider: Provider, model_id: str, project: WritingProject, system: str, user: str):
    req = CompletionRequest(
        model=model_id,
        system=system,
        messages=[Message(role="user", content=user)],
        max_tokens=project.config.max_tokens,
        temperature=project.config.temperature,
    )
    return provider.complete(req)


# ---------------------------------------------------------------------------
# Single-call protocol parser (invariant 7)
# ---------------------------------------------------------------------------

_SCENE_FENCE_RE = re.compile(r"```scene\s*\n(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)
_PRIVATE_RE = re.compile(r"^\s*(?P<slug>[\w-]+)\s*\(private\)>\s*(?P<note>.*)$", re.IGNORECASE)
_ACTION_RE = re.compile(r"^\s*(?P<slug>[\w-]+)\s*\[action\]\s*(?P<action>.*)$", re.IGNORECASE)
_SPEECH_RE = re.compile(r"^\s*(?P<slug>[\w-]+)>\s*(?P<speech>.*)$")


def parse_scene_block(text: str, slugs: list[str]) -> tuple[list[SceneTurn], list[str]]:
    """Parse a fenced ```scene block into the same turn-log shape U4 produces.

    Tolerant of surrounding prose. Each `SLUG> speech`, `SLUG [action] ...`,
    and `SLUG (private)> note` line becomes its own turn; private lines are
    excluded from the public log. Malformed lines and unknown slugs become
    parse notes (returned separately) rather than aborting. A missing fence is
    an actionable error.
    """
    match = _SCENE_FENCE_RE.search(text)
    if not match:
        raise SceneError("single-call scene output had no ```scene ... ``` block")
    known = {s.lower(): s for s in slugs}
    turns: list[SceneTurn] = []
    notes: list[str] = []
    for raw in match.group("body").splitlines():
        line = raw.strip()
        if not line:
            continue
        for regex, kind in ((_PRIVATE_RE, "private"), (_ACTION_RE, "action"), (_SPEECH_RE, "speech")):
            m = regex.match(line)
            if not m:
                continue
            slug = known.get(m.group("slug").lower())
            if slug is None:
                notes.append(f"unknown speaker in line: {line!r}")
                break
            if kind == "private":
                turns.append(SceneTurn(round=1, speaker=slug, private_note=m.group("note").strip()))
            elif kind == "action":
                turns.append(SceneTurn(round=1, speaker=slug, action=m.group("action").strip()))
            else:
                turns.append(SceneTurn(round=1, speaker=slug, speech=m.group("speech").strip()))
            break
        else:
            notes.append(f"unparseable line: {line!r}")
    return turns, notes


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


@dataclass
class _Transcript:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    def save(self) -> None:
        import json

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_scene(
    project: WritingProject,
    slugs: list[str],
    chapter: int,
    brief: str,
    model: str | None = None,
    provider: Provider | None = None,
    mode: str | None = None,
) -> SceneResult:
    """Run a scene simulation and return the assembled prose script.

    `slugs` are cast slugs (validated before any model call). `mode` overrides
    `config.cast.scene_mode` (auto|multi|single); auto picks single-call when
    the provider can't do tools, multi otherwise.
    """
    store = CastStore(project)
    missing = [s for s in slugs if not store.exists(s)]
    if missing:
        raise SceneError(f"no cast sheet(s) for: {', '.join(missing)}. Run `stoner cast init` first.")
    if len(slugs) < 2:
        raise SceneError("a scene needs at least two characters")

    sheets = {s: store.load(s) for s in slugs}
    cfg = project.config.cast
    prov, model_id = _resolve_provider(project, model, provider)

    chosen = mode or cfg.scene_mode
    if chosen == "auto":
        chosen = "single" if not prov.supports_tools else "multi"

    ts = time.strftime("%Y%m%dT%H%M%S")
    label = "-".join(slugs)[:60]
    transcript = _Transcript(
        path=project.resolve(f"{_SCENES_DIR}/{ts}-ch-{chapter:02d}-{label}.json"),
        data={
            "chapter": chapter,
            "brief": brief,
            "participants": slugs,
            "mode": chosen,
            "model": model_id,
            "started_at": time.time(),
            "turns": [],
        },
    )
    ledger = Ledger(project.root)
    ledger.append("cast.scene.start", target=f"ch-{chapter:02d}", participants=slugs, mode=chosen)

    usage = Usage()
    notes: list[str] = []

    if chosen == "single":
        turns, usage, stopped, notes = _run_single(
            project, prov, model_id, slugs, sheets, chapter, brief, transcript, ledger, usage
        )
    else:
        turns, usage, stopped = _run_multi(
            project, prov, model_id, slugs, sheets, chapter, brief, transcript, ledger, usage
        )

    script, asm_usage = _assemble(project, prov, model_id, chapter, brief, turns)
    usage = usage + asm_usage

    transcript.data["script"] = script
    transcript.data["usage"] = usage.model_dump()
    transcript.data["stopped_reason"] = stopped
    if notes:
        transcript.data["notes"] = notes
    transcript.save()

    ledger.append(
        "cast.scene.done",
        target=f"ch-{chapter:02d}",
        turns=len(turns),
        stopped_reason=stopped,
    )
    return SceneResult(
        script=script,
        transcript_path=str(transcript.path),
        mode=chosen,
        turns=turns,
        usage=usage,
        stopped_reason=stopped,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Multi-call path (U4)
# ---------------------------------------------------------------------------


def _character_system(
    project: WritingProject, slug: str, sheets: dict, chapter: int, brief: str
) -> str:
    sheet = sheets[slug]
    others = ", ".join(sheets[s].name for s in sheets if s != slug)
    return render_prompt(
        "cast_character.md",
        {
            "name": sheet.name,
            "slug": slug,
            "canon_voice": _canon_voice(project, slug),
            "private_state": private_digest(sheet, chapter, project.config.cast.sheet_digest_chars),
            "brief": brief,
            "others": others,
        },
    )


def _run_multi(
    project: WritingProject,
    provider: Provider,
    model_id: str,
    slugs: list[str],
    sheets: dict,
    chapter: int,
    brief: str,
    transcript: _Transcript,
    ledger: Ledger,
    usage: Usage,
) -> tuple[list[SceneTurn], Usage, str]:
    cfg = project.config.cast
    systems = {s: _character_system(project, s, sheets, chapter, brief) for s in slugs}
    turns: list[SceneTurn] = []
    stopped = "max_rounds"

    for rnd in range(1, cfg.scene_max_rounds + 1):
        all_passed = True
        for slug in slugs:
            public = _public_transcript(turns) or "(the scene has not started yet)"
            user = (
                f"## Scene so far\n\n{public}\n\n"
                "It is your turn. Respond with your move as the required JSON object."
            )
            turn = _one_multi_turn(project, provider, model_id, systems[slug], user, rnd, slug)
            usage = usage + turn[1]
            st = turn[0]
            turns.append(st)
            if not st.passed:
                all_passed = False
            transcript.data["turns"].append(st.model_dump())
            transcript.save()
            ledger.append(
                "cast.scene.turn", target=f"ch-{chapter:02d}", speaker=slug, passed=st.passed
            )
            if usage.input_tokens + usage.output_tokens >= cfg.scene_token_budget:
                stopped = "token_budget"
                return turns, usage, stopped
        if all_passed:
            stopped = "all_passed"
            return turns, usage, stopped
    return turns, usage, stopped


def _one_multi_turn(
    project: WritingProject,
    provider: Provider,
    model_id: str,
    system: str,
    user: str,
    rnd: int,
    slug: str,
) -> tuple[SceneTurn, Usage]:
    """One character turn: complete, parse, retry once, else raw-text fallback."""
    resp = _complete(provider, model_id, project, system, user)
    usage = resp.usage
    data = extract_json(resp.text)
    if not data:
        resp2 = _complete(provider, model_id, project, system, user + "\n\n" + _FORMAT_CORRECTION)
        usage = usage + resp2.usage
        data = extract_json(resp2.text)
        if not data:
            # Degrade, don't abort: keep the raw text as spoken lines.
            return SceneTurn(round=rnd, speaker=slug, speech=resp2.text.strip()), usage
    return (
        SceneTurn(
            round=rnd,
            speaker=slug,
            speech=str(data.get("speech") or "").strip(),
            action=str(data.get("action") or "").strip(),
            private_note=str(data.get("private_note") or "").strip(),
            passed=bool(data.get("pass", False)),
        ),
        usage,
    )


# ---------------------------------------------------------------------------
# Single-call path (U5)
# ---------------------------------------------------------------------------


def _run_single(
    project: WritingProject,
    provider: Provider,
    model_id: str,
    slugs: list[str],
    sheets: dict,
    chapter: int,
    brief: str,
    transcript: _Transcript,
    ledger: Ledger,
    usage: Usage,
) -> tuple[list[SceneTurn], Usage, str, list[str]]:
    cfg = project.config.cast
    blocks = []
    for slug in slugs:
        sheet = sheets[slug]
        voice = _canon_voice(project, slug)
        digest = private_digest(sheet, chapter, cfg.sheet_digest_chars)
        blocks.append(
            f"### {sheet.name} ({slug})\n"
            + (f"Voice: {voice}\n" if voice else "")
            + "Known only to "
            + slug.upper()
            + " — other characters must not reference or act on this:\n"
            + digest
        )
    system = render_prompt(
        "cast_scene_single.md",
        {
            "chapter_number": f"{chapter:02d}",
            "brief": brief,
            "hidden_state": "\n\n".join(blocks),
            "roster": ", ".join(f"{sheets[s].name} ({s.upper()})" for s in slugs),
        },
    )
    user = "Write the scene now as one ```scene block using the strict line protocol."
    resp = _complete(provider, model_id, project, system, user)
    usage = usage + resp.usage
    turns, notes = parse_scene_block(resp.text, slugs)
    for st in turns:
        transcript.data["turns"].append(st.model_dump())
        ledger.append(
            "cast.scene.turn", target=f"ch-{chapter:02d}", speaker=st.speaker, passed=st.passed
        )
    transcript.save()
    return turns, usage, "single_call", notes


# ---------------------------------------------------------------------------
# Assembly (mode-invariant)
# ---------------------------------------------------------------------------


def _assemble(
    project: WritingProject,
    provider: Provider,
    model_id: str,
    chapter: int,
    brief: str,
    turns: list[SceneTurn],
) -> tuple[str, Usage]:
    system = render_prompt("cast_scene_assemble.md", {"chapter_number": f"{chapter:02d}", "brief": brief})
    log = _public_transcript(turns) or "(no dialogue was produced)"
    user = f"## Scene brief\n\n{brief}\n\n## Turn log\n\n{log}\n\nRender this as a prose dialogue script."
    resp = _complete(provider, model_id, project, system, user)
    return resp.text.strip(), resp.usage
