"""Shared helpers for pipelines: prompt rendering, model calls, context."""

from __future__ import annotations

import re
from pathlib import Path

from ..config import StonerConfig
from ..project import WritingProject
from ..providers.base import Provider
from ..providers.registry import get_provider
from ..types import CompletionRequest, Message, Usage

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "engine" / "prompts"

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def render_prompt(template_name: str, values: dict[str, str]) -> str:
    """Fill `{placeholder}` slots in a prompt template.

    Only known placeholders are substituted (missing keys become "");
    any other braces in the template (JSON examples etc.) pass through
    untouched, which plain str.format would choke on.
    """
    text = (PROMPTS_DIR / template_name).read_text(encoding="utf-8")
    # Strip the leading HTML documentation comment.
    if text.startswith("<!--"):
        end = text.find("-->")
        if end != -1:
            text = text[end + 3 :].lstrip("\n")
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), text)


def resolve_role_model(config: StonerConfig, role: str, override: str | None = None) -> str:
    """Model string for a role: explicit override > config.models.<role>."""
    if override:
        return override
    model = getattr(config.models, role, "")
    if not model:
        raise ValueError(f"No model configured for role {role!r} in stoner.yaml")
    return model


def call_model(
    project: WritingProject,
    role: str,
    system: str,
    user: str,
    model: str | None = None,
    provider: Provider | None = None,
    max_tokens: int | None = None,
) -> tuple[str, Usage]:
    """One plain (no-tools) completion for a configured role."""
    model_str = resolve_role_model(project.config, role, model)
    if provider is None:
        provider, model_id = get_provider(model_str, project.config)
    else:
        _, model_id = model_str.split("/", 1) if "/" in model_str else ("", model_str)
    req = CompletionRequest(
        model=model_id,
        system=system,
        messages=[Message(role="user", content=user)],
        max_tokens=max_tokens or project.config.max_tokens,
        temperature=project.config.temperature,
    )
    resp = provider.complete(req)
    return resp.text, resp.usage


def chapter_context(project: WritingProject, number: int) -> dict[str, str]:
    """Assemble the standard placeholder values for chapter-level prompts."""
    from ..canon.memory import Memory
    from ..canon.store import CanonStore

    store = CanonStore(project)
    memory = Memory(project)

    def read_or_empty(rel: str) -> str:
        try:
            return project.read(rel)
        except Exception:
            return ""

    beats = read_or_empty(f"outline/beats/ch-{number:02d}.md")
    previous_tail = ""
    if number > 1:
        try:
            _, prev_body = project.read_chapter(number - 1)
            previous_tail = prev_body[-3000:]
        except Exception:
            previous_tail = ""
    threads = "\n".join(
        f"- [{t.id}] {t.thread} (opened {t.opened_in}, {t.status})"
        for t in store.threads()
        if t.status == "open"
    )
    return {
        "project_name": project.config.project_name,
        "style_guide": read_or_empty("canon/style.md"),
        "premise": read_or_empty("canon/premise.md"),
        "chapter_number": f"{number:02d}",
        "beats": beats,
        "memory_summary": memory.context_for_chapter(number),
        "previous_tail": previous_tail,
        "threads": threads,
        "existing_canon": store.context_pack(max_chars=8000),
    }
