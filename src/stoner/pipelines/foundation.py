"""The foundation pipeline: seed -> brainstorm -> canon (the story bible).

The autonovel Phase-1 analog (see `docs/research/RESEARCH.md`,
"NousResearch/autonovel" — "Foundation loop (exit on scores)"): before a
single chapter is drafted, turn a one-line seed into a premise and style
(`run_brainstorm`), then build the rest of the bible — characters, world,
threads, outline — as a sequence of small, separately-prompted model calls
so context stays bounded (`run_canon_generation`). A final comparative
evaluate step (never absolute 1-10 scoring — see RESEARCH.md "Evaluation
insight") picks the single weakest element and, if it's weak enough to
hurt drafting, regenerates just that element with the model's own fix
instructions, up to `max_loops` times.

Every write in this module goes through the existing template structure in
`canon/templates/` (via `canon.scaffold.new_canon_entry` / `new_beats_stub`
and `canon.store.CanonStore`) so hand-editing the result later feels the
same as editing any other canon file, and the slop detector's `## Banned`
parser keeps working unmodified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..canon.scaffold import new_beats_stub, new_canon_entry
from ..canon.store import CanonStore, extract_section, parse_table, render_table, slugify
from ..ledger import Ledger
from ..project import WritingProject, join_frontmatter, split_frontmatter
from ..providers.base import Provider
from ..review.passes import extract_json
from ..types import Usage
from .common import call_model, render_prompt, resolve_role_model

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "canon" / "templates"


class FoundationError(RuntimeError):
    """Raised for foundation-pipeline precondition/parse failures."""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class BrainstormResult:
    premise: dict[str, Any]
    style: dict[str, Any]
    title_options: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    written: list[str] = field(default_factory=list)


@dataclass
class FoundationResult:
    characters: list[str] = field(default_factory=list)
    world: list[str] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)
    chapters: list[int] = field(default_factory=list)
    loops: int = 0
    verdict: str = "ship"
    usage: Usage = field(default_factory=Usage)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Model role: "foundation" falls back to the writer role (config.py is not
# touched -- there is no models.foundation field, so we just always resolve
# against "writer").
# ---------------------------------------------------------------------------

_ROLE = "writer"


def resolve_foundation_model(project: WritingProject, override: str | None = None) -> str:
    """Model string for the foundation pipeline: override > writer role."""
    return resolve_role_model(project.config, _ROLE, override)


def _model_call(
    project: WritingProject,
    system: str,
    user: str,
    model: str | None,
    provider: Provider | None,
) -> tuple[str, Usage]:
    return call_model(project, _ROLE, system, user, model=model, provider=provider)


# ---------------------------------------------------------------------------
# Template helpers (read canon/templates/*.md directly; canon/scaffold.py
# and canon/store.py are contracts we import from, not edit).
# ---------------------------------------------------------------------------


def _read_canon_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _premise_template_text(project: WritingProject) -> str:
    return _read_canon_template("premise.md").replace("{{PROJECT_NAME}}", project.config.project_name)


def _style_template_text() -> str:
    return _read_canon_template("style.md")


def _is_template_text(current: str, template: str, heading: str) -> bool:
    """True if `current`'s `heading` section still matches the pristine
    template's (i.e. still just the instructive placeholder comment)."""
    return extract_section(current, heading).strip() == extract_section(template, heading).strip()


def _premise_is_filled(project: WritingProject) -> bool:
    try:
        current = project.read("canon/premise.md")
    except Exception:
        return False
    return not _is_template_text(current, _premise_template_text(project), "Logline")


def _fill_section(text: str, heading: str, value: str) -> str:
    """Fill a `## heading` section's blank body with `value`, keeping any
    leading HTML instructive comment from the template intact."""
    pattern = re.compile(
        rf"(^##[ \t]+{re.escape(heading)}[ \t]*\n)(.*?)(?=^##[ \t]+|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        return text
    body = m.group(2)
    value = value.strip()
    comment_m = re.search(r"<!--.*?-->", body, re.DOTALL)
    comment = comment_m.group(0) if comment_m else ""
    pieces = [p for p in (comment, value) if p]
    new_body = ("\n" + "\n\n".join(pieces) + "\n\n") if pieces else body
    return text[: m.start(2)] + new_body + text[m.end(2) :]


def _fill_list_line(text: str, label: str, value: str) -> str:
    """Fill a `- Label: <!-- comment -->` line in place, keeping the comment."""
    pattern = re.compile(rf"^(-\s*{re.escape(label)}:)(.*)$", re.MULTILINE)

    def repl(m: re.Match[str]) -> str:
        return f"{m.group(1)} {value}{m.group(2)}"

    return pattern.sub(repl, text, count=1)


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _fill_banned(text: str, words: list[str], phrases: list[str]) -> str:
    """Replace the `## Banned` fenced-yaml block's words/phrases, merging in
    whatever defaults were already there (model-provided terms first)."""
    m = re.search(r"(## Banned.*?```(?:yaml|yml)?\n)(.*?)(```)", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return text
    try:
        existing = yaml.safe_load(m.group(2)) or {}
    except yaml.YAMLError:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    default_words = [str(w) for w in (existing.get("words") or [])]
    default_phrases = [str(p) for p in (existing.get("phrases") or [])]
    merged_words = _dedup([*words, *default_words])
    merged_phrases = _dedup([*phrases, *default_phrases])
    block = yaml.safe_dump({"words": merged_words, "phrases": merged_phrases}, sort_keys=False, allow_unicode=True)
    return text[: m.start(2)] + block + text[m.end(2) :]


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Brainstorm: seed -> premise.md + style.md
# ---------------------------------------------------------------------------


def _render_premise(project: WritingProject, premise: dict[str, Any]) -> str:
    text = _premise_template_text(project)
    text = _fill_section(text, "Logline", str(premise.get("logline", "")))
    text = _fill_list_line(text, "Genre", str(premise.get("genre", "")))
    comps = [str(c) for c in (premise.get("comps") or [])]
    text = _fill_list_line(text, "Comps", ", ".join(comps))
    themes = [str(t) for t in (premise.get("themes") or [])]
    text = _fill_section(text, "Themes", "\n".join(f"- {t}" for t in themes))
    text = _fill_section(text, "Promise to the Reader", str(premise.get("promise", "")))
    return text


def _render_style(style: dict[str, Any]) -> str:
    text = _style_template_text()
    text = _fill_section(text, "Voice", str(style.get("voice", "")))
    text = _fill_list_line(text, "Point of view", str(style.get("pov", "")))
    text = _fill_list_line(text, "Tense", str(style.get("tense", "")))
    text = _fill_section(text, "Sentence Rhythm", str(style.get("rhythm_notes", "")))
    words = [str(w) for w in (style.get("banned_words") or [])]
    phrases = [str(p) for p in (style.get("banned_phrases") or [])]
    text = _fill_banned(text, words, phrases)
    return text


def run_brainstorm(
    project: WritingProject,
    seed: str,
    model: str | None = None,
    provider: Provider | None = None,
    force: bool = False,
) -> BrainstormResult:
    """Seed -> premise + style, written into canon/premise.md and canon/style.md."""
    seed = seed.strip()
    if not seed:
        raise FoundationError("seed must not be empty")

    if not force:
        try:
            cur_premise = project.read("canon/premise.md")
        except Exception:
            cur_premise = _premise_template_text(project)
        try:
            cur_style = project.read("canon/style.md")
        except Exception:
            cur_style = _style_template_text()
        premise_untouched = _is_template_text(cur_premise, _premise_template_text(project), "Logline")
        style_untouched = _is_template_text(cur_style, _style_template_text(), "Voice")
        if not (premise_untouched and style_untouched):
            raise FoundationError(
                "canon/premise.md and/or canon/style.md already have content beyond "
                "the template. Re-run `stoner brainstorm` with --force to overwrite, "
                "or edit them by hand instead."
            )

    system = render_prompt("brainstorm.md", {"project_name": project.config.project_name})
    user = f"Seed idea:\n\n{seed}"
    text, usage = _model_call(project, system, user, model, provider)
    data = extract_json(text)
    premise = data.get("premise") if isinstance(data, dict) else None
    style = data.get("style") if isinstance(data, dict) else None
    if not isinstance(premise, dict) or not isinstance(style, dict):
        raise FoundationError(
            "Model response did not contain the expected "
            "{'premise': {...}, 'style': {...}} JSON."
        )
    title_options = [str(t).strip() for t in (data.get("title_options") or []) if str(t).strip()]

    project.write("canon/premise.md", _render_premise(project, premise))
    project.write("canon/style.md", _render_style(style))

    Ledger(project.root).append(
        "foundation.brainstorm",
        target="canon/premise.md",
        seed=seed[:200],
        titles=len(title_options),
    )
    return BrainstormResult(
        premise=premise,
        style=style,
        title_options=title_options,
        usage=usage,
        written=["canon/premise.md", "canon/style.md"],
    )


# ---------------------------------------------------------------------------
# Canon generation: characters / world / threads / outline
# ---------------------------------------------------------------------------


def _shared_ctx(project: WritingProject, store: CanonStore, extra: str) -> dict[str, str]:
    return {
        "project_name": project.config.project_name,
        "premise": project.read("canon/premise.md"),
        "style_guide": project.read("canon/style.md"),
        "existing_canon": store.context_pack(max_chars=6000),
        "extra_instructions": extra,
    }


def _generate_characters(
    project: WritingProject,
    store: CanonStore,
    n: int,
    model: str | None,
    provider: Provider | None,
    extra: str = "",
) -> tuple[list[dict[str, Any]], Usage]:
    ctx = _shared_ctx(project, store, extra)
    ctx["count"] = str(n)
    system = render_prompt("foundation_characters.md", ctx)
    user = f"Generate {n} characters now as STRICT JSON."
    text, usage = _model_call(project, system, user, model, provider)
    data = extract_json(text)
    items = data.get("characters") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise FoundationError("model did not return a non-empty 'characters' JSON list")
    return [it for it in items if isinstance(it, dict)][:n], usage


def _write_characters(store: CanonStore, items: list[dict[str, Any]]) -> list[str]:
    slugs: list[str] = []
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        _rel, template_content = new_canon_entry("character", name)
        _fm, body = split_frontmatter(template_content)
        body = _fill_section(body, "Voice", str(item.get("voice", "")))
        wants = str(item.get("wants", "")).strip()
        fears = str(item.get("fears", "")).strip()
        wf_lines = []
        if wants:
            wf_lines.append(f"Wants: {wants}")
        if fears:
            wf_lines.append(f"Fears: {fears}")
        body = _fill_section(body, "Wants / Fears", "\n\n".join(wf_lines))
        body = _fill_section(body, "Arc", str(item.get("arc", "")))

        appearance_raw = item.get("appearance")
        appearance = appearance_raw if isinstance(appearance_raw, dict) else {}
        relationships_raw = item.get("relationships")
        relationships = relationships_raw if isinstance(relationships_raw, dict) else {}
        fm_updates = {
            "name": name,
            "role": str(item.get("role", "")),
            "age": item.get("age", ""),
            "appearance": {
                "hair": str(appearance.get("hair", "")),
                "eyes": str(appearance.get("eyes", "")),
                "build": str(appearance.get("build", "")),
                "distinguishing": str(appearance.get("distinguishing", "")),
            },
            "relationships": {str(k): str(v) for k, v in relationships.items()},
            "status": "alive",
        }
        slug = slugify(name)
        store.upsert_character(slug, fm_updates, body=body)
        slugs.append(slug)
    return slugs


def _generate_world(
    project: WritingProject,
    store: CanonStore,
    n: int,
    model: str | None,
    provider: Provider | None,
    extra: str = "",
) -> tuple[list[dict[str, Any]], Usage]:
    ctx = _shared_ctx(project, store, extra)
    ctx["count"] = str(n)
    system = render_prompt("foundation_world.md", ctx)
    user = f"Generate {n} world entries now as STRICT JSON."
    text, usage = _model_call(project, system, user, model, provider)
    data = extract_json(text)
    items = data.get("world") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise FoundationError("model did not return a non-empty 'world' JSON list")
    return [it for it in items if isinstance(it, dict)][:n], usage


_WORLD_TYPES = {"place", "faction", "system", "item"}


def _write_world(store: CanonStore, items: list[dict[str, Any]]) -> list[str]:
    slugs: list[str] = []
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        _rel, template_content = new_canon_entry("world", name)
        _fm, body = split_frontmatter(template_content)
        body = _fill_section(body, "Rules", str(item.get("rules", "")))
        body = _fill_section(body, "Description", str(item.get("description", "")))
        body = _fill_section(body, "History", str(item.get("history", "")))

        wtype = str(item.get("type", "") or "place").strip().lower()
        if wtype not in _WORLD_TYPES:
            wtype = "place"
        slug = slugify(name)
        store.upsert_world(slug, {"name": name, "type": wtype}, body=body)
        slugs.append(slug)
    return slugs


def _generate_threads(
    project: WritingProject,
    store: CanonStore,
    model: str | None,
    provider: Provider | None,
    extra: str = "",
) -> tuple[list[dict[str, Any]], Usage]:
    ctx = _shared_ctx(project, store, extra)
    system = render_prompt("foundation_threads.md", ctx)
    user = "Generate 5-8 opening plot threads now as STRICT JSON."
    text, usage = _model_call(project, system, user, model, provider)
    data = extract_json(text)
    items = data.get("threads") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise FoundationError("model did not return a non-empty 'threads' JSON list")
    return [it for it in items if isinstance(it, dict)][:8], usage


def _write_threads(project: WritingProject, store: CanonStore, items: list[dict[str, Any]]) -> list[str]:
    # Reset to the blank template first so a regeneration (idempotence
    # bypass on evaluate-loop iterate, or --force) doesn't collide with, or
    # duplicate, a prior run's thread ids.
    project.write("canon/threads.md", _read_canon_template("threads.md"))
    ids: list[str] = []
    for i, item in enumerate(items, start=1):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        tid = f"t{i}"
        store.add_thread(
            tid,
            name,
            opened_in=str(item.get("opened_in", "") or "ch-01"),
            status="open",
            notes=str(item.get("notes", "")),
        )
        ids.append(tid)
    return ids


def _generate_outline(
    project: WritingProject,
    store: CanonStore,
    model: str | None,
    provider: Provider | None,
    extra: str = "",
) -> tuple[dict[str, Any], Usage]:
    ctx = _shared_ctx(project, store, extra)
    system = render_prompt("foundation_outline.md", ctx)
    user = "Generate the act structure and chapter list now as STRICT JSON."
    text, usage = _model_call(project, system, user, model, provider)
    data = extract_json(text)
    chapters = data.get("chapters") if isinstance(data, dict) else None
    if not isinstance(chapters, list) or not chapters:
        raise FoundationError("model did not return a non-empty 'chapters' JSON list")
    return data, usage


def _write_outline(project: WritingProject, data: dict[str, Any]) -> list[int]:
    acts_raw = data.get("acts")
    acts = acts_raw if isinstance(acts_raw, dict) else {}
    text = _read_canon_template("outline.md")
    text = _fill_section(text, "Act I — Setup", str(acts.get("act1", "")))
    text = _fill_section(text, "Act II — Confrontation", str(acts.get("act2", "")))
    text = _fill_section(text, "Act III — Resolution", str(acts.get("act3", "")))

    chapters = [c for c in data.get("chapters", []) if isinstance(c, dict)]
    chapters.sort(key=lambda c: _int_or(c.get("number"), 0))

    prefix, headers, _rows, suffix = parse_table(text)
    rows = []
    numbers: list[int] = []
    for i, c in enumerate(chapters, start=1):
        number = _int_or(c.get("number"), i)
        numbers.append(number)
        pov = str(c.get("pov", ""))
        summary = " ".join(str(c.get("summary", "")).split())
        rows.append([str(number), pov, summary])
    project.write("outline/outline.md", render_table(prefix, headers, rows, suffix))

    for c in chapters:
        number = _int_or(c.get("number"), 0)
        if number <= 0:
            continue
        beats_raw = c.get("beats")
        beats = beats_raw if isinstance(beats_raw, dict) else {}
        stub = new_beats_stub(number)
        fm, body = split_frontmatter(stub)
        fm["pov"] = str(c.get("pov", ""))
        body = _fill_section(body, "Goal", str(beats.get("goal", "")))
        body = _fill_section(body, "Conflict", str(beats.get("conflict", "")))
        body = _fill_section(body, "Turn", str(beats.get("turn", "")))
        body = _fill_section(body, "Exit State", str(beats.get("exit_state", "")))
        project.write(f"outline/beats/ch-{number:02d}.md", join_frontmatter(fm, body))
    return numbers


def _outline_is_filled(project: WritingProject) -> bool:
    try:
        current = project.read("outline/outline.md")
    except Exception:
        return False
    if not current.strip():
        return False
    return not _is_template_text(current, _read_canon_template("outline.md"), "Chapter Map")


def _existing_beat_chapters(project: WritingProject) -> list[int]:
    beats_dir = project.root / "outline" / "beats"
    if not beats_dir.exists():
        return []
    out = []
    for f in beats_dir.glob("ch-*.md"):
        m = re.match(r"ch-(\d+)\.md$", f.name)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


# ---------------------------------------------------------------------------
# Evaluate loop
# ---------------------------------------------------------------------------


def _foundation_digest(project: WritingProject, store: CanonStore) -> str:
    parts = [store.context_pack(max_chars=8000)]
    try:
        outline_text = project.read("outline/outline.md")
    except Exception:
        outline_text = ""
    if outline_text.strip():
        parts.append(f"## Outline\n\n{outline_text}")
    return "\n\n".join(p for p in parts if p.strip())


def _evaluate(
    project: WritingProject, digest: str, model: str | None, provider: Provider | None
) -> tuple[dict[str, Any], Usage]:
    system = render_prompt("foundation_evaluate.md", {"project_name": project.config.project_name})
    user = f"## Foundation digest\n\n{digest}\n\nEvaluate now and respond with STRICT JSON."
    text, usage = _model_call(project, system, user, model, provider)
    data = extract_json(text)
    if not isinstance(data, dict) or "verdict" not in data:
        raise FoundationError("evaluate step did not return the expected JSON shape")
    return data, usage


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_canon_generation(
    project: WritingProject,
    model: str | None = None,
    provider: Provider | None = None,
    characters: int = 4,
    world_entries: int = 3,
    force: bool = False,
    max_loops: int = 2,
) -> FoundationResult:
    """Build characters/world/threads/outline from a filled premise, then
    run one comparative evaluate-and-regenerate loop over the weakest one."""
    if not _premise_is_filled(project):
        raise FoundationError(
            "canon/premise.md is still the blank template -- run `stoner brainstorm` "
            "first (or fill it in by hand) before generating canon."
        )

    store = CanonStore(project)
    ledger = Ledger(project.root)
    result = FoundationResult()

    # -- characters -----------------------------------------------------
    existing_chars = store.list_entries(kind="character")
    if existing_chars and not force:
        result.characters = [e.slug for e in existing_chars]
        result.notes.append(f"characters: skipped ({len(existing_chars)} already exist)")
    else:
        items, usage = _generate_characters(project, store, characters, model, provider)
        result.usage += usage
        result.characters = _write_characters(store, items)
        ledger.append("foundation.characters", target="canon/characters", count=len(result.characters))

    # -- world ------------------------------------------------------------
    existing_world = store.list_entries(kind="world")
    if existing_world and not force:
        result.world = [e.slug for e in existing_world]
        result.notes.append(f"world: skipped ({len(existing_world)} already exist)")
    else:
        items, usage = _generate_world(project, store, world_entries, model, provider)
        result.usage += usage
        result.world = _write_world(store, items)
        ledger.append("foundation.world", target="canon/world", count=len(result.world))

    # -- threads ------------------------------------------------------------
    existing_threads = store.threads()
    if existing_threads and not force:
        result.threads = [t.id for t in existing_threads]
        result.notes.append(f"threads: skipped ({len(existing_threads)} already exist)")
    else:
        items, usage = _generate_threads(project, store, model, provider)
        result.usage += usage
        result.threads = _write_threads(project, store, items)
        ledger.append("foundation.threads", target="canon/threads.md", count=len(result.threads))

    # -- outline ------------------------------------------------------------
    if _outline_is_filled(project) and not force:
        result.chapters = _existing_beat_chapters(project)
        result.notes.append("outline: skipped (already has content)")
    else:
        data, usage = _generate_outline(project, store, model, provider)
        result.usage += usage
        result.chapters = _write_outline(project, data)
        ledger.append("foundation.outline", target="outline/outline.md", count=len(result.chapters))

    # -- evaluate loop --------------------------------------------------
    digest = _foundation_digest(project, store)
    verdict_data, usage = _evaluate(project, digest, model, provider)
    result.usage += usage
    loops = 0
    while verdict_data.get("verdict") == "iterate" and loops < max_loops:
        weakest = str(verdict_data.get("weakest_element", "")).strip().lower()
        why = str(verdict_data.get("why", ""))
        fix = str(verdict_data.get("fix_instructions", ""))
        extra = f"The previous {weakest or 'element'} generation was judged weakest: {why}\n\nRevise as follows: {fix}"

        if weakest == "characters":
            items, u = _generate_characters(project, store, characters, model, provider, extra=extra)
            result.usage += u
            result.characters = _write_characters(store, items)
        elif weakest == "world":
            items, u = _generate_world(project, store, world_entries, model, provider, extra=extra)
            result.usage += u
            result.world = _write_world(store, items)
        elif weakest == "threads":
            items, u = _generate_threads(project, store, model, provider, extra=extra)
            result.usage += u
            result.threads = _write_threads(project, store, items)
        elif weakest == "outline":
            data, u = _generate_outline(project, store, model, provider, extra=extra)
            result.usage += u
            result.chapters = _write_outline(project, data)
        else:
            break

        loops += 1
        ledger.append("foundation.evaluate.iterate", target=f"canon/{weakest}", loop=loops, why=why)
        digest = _foundation_digest(project, store)
        verdict_data, usage2 = _evaluate(project, digest, model, provider)
        result.usage += usage2

    result.loops = loops
    result.verdict = str(verdict_data.get("verdict", "ship"))

    ledger.append(
        "foundation.done",
        target="canon",
        loops=loops,
        verdict=result.verdict,
        characters=len(result.characters),
        world=len(result.world),
        threads=len(result.threads),
        chapters=len(result.chapters),
    )
    return result
