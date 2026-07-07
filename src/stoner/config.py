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

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, project_root: Path, overrides: dict[str, Any] | None = None) -> "StonerConfig":
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
