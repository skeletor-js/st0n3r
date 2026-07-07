"""Model-string parsing and provider construction.

Model strings look like `<provider>/<model-id>`, e.g.:
    anthropic/claude-sonnet-5
    openai/gpt-5.2
    openrouter/deepseek/deepseek-chat   (first segment picks the provider entry)
    ollama/llama3.3
    codex/gpt-5-codex
    claude/claude-sonnet-5

The provider segment is looked up in StonerConfig.providers; unknown names
fall back to built-in defaults for `anthropic`, `openai`, `openrouter`,
`ollama`, `codex`, and `claude`.
"""

from __future__ import annotations

from ..config import ProviderConfig, StonerConfig
from .base import Provider, ProviderError

BUILTIN_PROVIDERS: dict[str, ProviderConfig] = {
    "anthropic": ProviderConfig(kind="anthropic", api_key_env="ANTHROPIC_API_KEY"),
    "openai": ProviderConfig(kind="openai_compat", api_key_env="OPENAI_API_KEY"),
    "openrouter": ProviderConfig(
        kind="openai_compat",
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
    ),
    "together": ProviderConfig(
        kind="openai_compat",
        api_key_env="TOGETHER_API_KEY",
        base_url="https://api.together.xyz/v1",
    ),
    "groq": ProviderConfig(
        kind="openai_compat",
        api_key_env="GROQ_API_KEY",
        base_url="https://api.groq.com/openai/v1",
    ),
    "ollama": ProviderConfig(kind="openai_compat", base_url="http://localhost:11434/v1"),
    "codex": ProviderConfig(kind="codex_cli"),
    "claude": ProviderConfig(kind="claude_code"),
}


def parse_model_string(model: str) -> tuple[str, str]:
    """Split `provider/model-id` (model-id may itself contain slashes)."""
    if "/" not in model:
        raise ProviderError(
            f"Model string {model!r} must be '<provider>/<model>' "
            "(e.g. anthropic/claude-sonnet-5, openai/gpt-5.2, codex/gpt-5-codex)"
        )
    provider, model_id = model.split("/", 1)
    return provider.lower(), model_id


def get_provider(model: str, config: StonerConfig) -> tuple[Provider, str]:
    """Return (provider instance, bare model id) for a model string."""
    pname, model_id = parse_model_string(model)
    pc = config.providers.get(pname) or BUILTIN_PROVIDERS.get(pname)
    if pc is None:
        known = sorted(set(BUILTIN_PROVIDERS) | set(config.providers))
        raise ProviderError(
            f"Unknown provider {pname!r}. Known: {', '.join(known)}. "
            "Add a custom entry under `providers:` in stoner.yaml."
        )
    if pc.kind == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(pc), model_id
    if pc.kind == "openai_compat":
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(pname, pc), model_id
    if pc.kind == "codex_cli":
        from .codex_cli import CodexCLIProvider

        return CodexCLIProvider(pc), model_id
    if pc.kind == "claude_code":
        from .claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider(pc), model_id
    raise ProviderError(f"Unknown provider kind {pc.kind!r} for {pname!r}")
