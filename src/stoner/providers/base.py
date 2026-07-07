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

    @abstractmethod
    def complete(self, req: CompletionRequest) -> CompletionResponse:
        """Run one model call. Must raise ProviderError with a clear, actionable
        message on auth/transport failure (never leak raw tracebacks to CLI)."""
