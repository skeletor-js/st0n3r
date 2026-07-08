"""Provider interface. Nothing outside stoner.providers imports vendor SDKs."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..types import CompletionRequest, CompletionResponse


class ProviderError(RuntimeError):
    """Raised for auth/config/transport failures with an actionable message."""


class Provider(ABC):
    """A chat-completion backend.

    `supports_tools` tells the engine whether to send native tool specs or to
    fall back to the fenced-JSON tool protocol.
    """

    name: str = "base"
    supports_tools: bool = True
    # Whether this provider can honor a `CompletionRequest.web_search` spec
    # (native/server-side web search). Providers that leave this False MUST
    # raise `ProviderError` when handed a spec rather than silently ignoring
    # it -- pretending to source facts is worse than failing loudly.
    supports_web_search: bool = False

    @abstractmethod
    def complete(self, req: CompletionRequest) -> CompletionResponse:
        """Run one model call. Must raise ProviderError with a clear, actionable
        message on auth/transport failure (never leak raw tracebacks to CLI)."""
