"""Configuration loading for a st0n3r writing project.

Resolution order (later wins): built-in defaults -> stoner.yaml -> env vars
-> explicit overrides (CLI flags).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

CONFIG_FILENAME = "stoner.yaml"


class ProviderConfig(BaseModel):
    """Connection settings for one named provider entry."""

    kind: str = "anthropic"  # anthropic | openai_compat | codex_cli
    api_key_env: str = ""  # env var holding the key (never store keys in yaml)
    base_url: str = ""  # openai_compat only
    extra: dict[str, Any] = Field(default_factory=dict)


class GateConfig(BaseModel):
    """Quality gates applied during the write pipeline."""

    slop_max_score: float = 25.0
    slop_block_severities: list[str] = Field(default_factory=lambda: ["critical"])
    max_revision_loops: int = 2


def _default_voice_weights() -> dict[str, float]:
    # Imported lazily (at model instantiation, not module import) so the
    # bucket weights can live next to the scoring curve they tune in
    # voice/drift.py without creating a config <-> voice import cycle.
    from .voice.drift import DEFAULT_WEIGHTS

    return dict(DEFAULT_WEIGHTS)


class VoiceConfig(BaseModel):
    """Voice-fingerprint learning and drift-gate settings (src/stoner/voice/)."""

    exemplars: list[str] = Field(default_factory=lambda: ["notes/exemplars"])
    gate: bool = False  # opt-in: gate drafts on measured voice drift
    max_drift_score: float = 40.0
    weights: dict[str, float] = Field(default_factory=_default_voice_weights)


class PacingConfig(BaseModel):
    """Thresholds for the advisory pacing instrument layer (never gating)."""

    in_scene_min_ratio: float = 0.70
    ending_echo_min_run: int = 3
    flatline_min_run: int = 3
    pov_break_min_run: int = 4
    llm_instruments: bool = True


class EditorSpec(BaseModel):
    """One named editor in the Writers' Room roster (src/stoner/room/).

    An editor is a persona (prepended to the wrapped pass system prompt) plus
    a set of existing review-pass names it runs. Unknown pass names are soft:
    they warn and are skipped at session time, mirroring the runner's
    unknown-pass degradation -- so a roster stays loadable even if a pass was
    renamed or belongs to a not-yet-installed feature.
    """

    name: str
    persona: str = ""
    passes: list[str] = Field(default_factory=list)


def _default_editors() -> list[EditorSpec]:
    # Defined as a factory (not a module constant) so each RoomConfig gets its
    # own EditorSpec instances -- pydantic would otherwise share mutable list
    # fields across configs.
    return [
        EditorSpec(
            name="Developmental Editor",
            persona=(
                "You are the developmental editor: you care about structure, "
                "momentum, and whether scenes earn their place. You think in "
                "chapters and arcs, not sentences."
            ),
            passes=["pacing", "logic"],
        ),
        EditorSpec(
            name="Line Editor",
            persona=(
                "You are the line editor: you hunt prose-level tells sentence "
                "by sentence and you are ruthless about cutting what does not "
                "need to exist. Praise is worthless; find what can go."
            ),
            passes=["line", "adversarial"],
        ),
        EditorSpec(
            name="Continuity Pedant",
            persona=(
                "You are the continuity pedant: you cross-check every chapter "
                "against canon and the story so far, and nothing escapes you -- "
                "eye colors, timelines, who knew what when."
            ),
            passes=["continuity"],
        ),
        EditorSpec(
            name="First Reader",
            persona=(
                "You are the first reader: you judge the chapter as a reader "
                "encountering it fresh, paragraph by paragraph, with no stake "
                "in defending any of it."
            ),
            passes=["grade"],
        ),
    ]


class RoomConfig(BaseModel):
    """The Writers' Room: a persistent roster of editors (src/stoner/room/).

    The roster is the cost knob (R17): a chapter session costs one call per
    editor per assigned pass, one cross-examination per editor, plus at most
    one re-locate fallback and one comment follow-up. `model` empty resolves
    against the reviewer role; set it to run the room on a cheaper model.
    """

    editors: list[EditorSpec] = Field(default_factory=_default_editors)
    model: str = ""  # empty resolves against models.reviewer
    opinion_cap_chars: int = 2000
    digest_chars: int = 3000
    max_open_items: int = 50
    max_resolved_items: int = 20
    llm_relocate: bool = True


class ArchaeologyConfig(BaseModel):
    """Draft snapshot store (`.stoner/drafts/`) behavior."""

    enabled: bool = True
    dedup: bool = True
    keep_per_chapter: int | None = None  # `stoner drafts prune` default
    verify_after_refactor: bool = True


class ModelRoles(BaseModel):
    """Which model handles which job. Any `provider/model` string."""

    writer: str = "anthropic/claude-sonnet-5"
    reviewer: str = "anthropic/claude-sonnet-5"
    archivist: str = "anthropic/claude-haiku-4-5-20251001"


class StonerConfig(BaseModel):
    project_name: str = "untitled"
    models: ModelRoles = Field(default_factory=ModelRoles)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    gates: GateConfig = Field(default_factory=GateConfig)
    review_passes: list[str] = Field(
        default_factory=lambda: ["continuity", "pacing", "voice", "line"]
    )
    max_tokens: int = 8192
    temperature: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    pacing: PacingConfig = Field(default_factory=PacingConfig)
    room: RoomConfig = Field(default_factory=RoomConfig)
    archaeology: ArchaeologyConfig = Field(default_factory=ArchaeologyConfig)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, project_root: Path, overrides: dict[str, Any] | None = None) -> StonerConfig:
        path = project_root / CONFIG_FILENAME
        data: dict[str, Any] = {}
        if path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{path} must contain a YAML mapping")
            data = loaded
        cfg = cls.model_validate(data)
        cfg._apply_env()
        if overrides:
            cfg = cfg.model_copy(update={k: v for k, v in overrides.items() if v is not None})
        return cfg

    def _apply_env(self) -> None:
        env_writer = os.environ.get("STONER_MODEL")
        if env_writer:
            self.models.writer = env_writer

    def dump_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(exclude_defaults=False, exclude={"extra"}),
            sort_keys=False,
            allow_unicode=True,
        )


def resolve_api_key(pc: ProviderConfig, default_envs: list[str]) -> str:
    """Find an API key: explicit env name first, then conventional fallbacks."""
    candidates = ([pc.api_key_env] if pc.api_key_env else []) + default_envs
    for env in candidates:
        val = os.environ.get(env, "")
        if val:
            return val
    return ""
