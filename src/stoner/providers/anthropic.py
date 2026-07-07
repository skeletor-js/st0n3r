"""Anthropic Messages API provider.

Maps the harness's provider-agnostic `Message`/`ToolSpec`/`ToolCall`/
`ToolResult` shapes onto Anthropic's content-block based Messages API and
back into a `CompletionResponse`.
"""

from __future__ import annotations

from typing import Any

from ..config import ProviderConfig, resolve_api_key
from ..types import CompletionRequest, CompletionResponse, Message, ToolCall, Usage
from .base import Provider, ProviderError

_DEFAULT_MAX_TOKENS = 8192


class AnthropicProvider(Provider):
    """Provider backed by the `anthropic` SDK (Claude models)."""

    name = "anthropic"
    supports_tools = True

    def __init__(self, pc: ProviderConfig):
        self.pc = pc
        api_key = resolve_api_key(pc, ["ANTHROPIC_API_KEY"])
        if not api_key:
            raise ProviderError(
                "No Anthropic API key found. Set ANTHROPIC_API_KEY "
                f"{f'(or {pc.api_key_env}) ' if pc.api_key_env else ''}"
                "in your environment, or add `api_key_env:` under this provider "
                "in stoner.yaml."
            )
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - packaging issue, not test path
            raise ProviderError(
                "The `anthropic` package is not installed. Install it with "
                "`pip install st0n3r[anthropic]` or `pip install anthropic`."
            ) from e
        self._anthropic = anthropic
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if pc.base_url:
            client_kwargs["base_url"] = pc.base_url
        self.client = anthropic.Anthropic(**client_kwargs)

    # -- request building -------------------------------------------------
    @staticmethod
    def _tool_specs_to_anthropic(tools: list) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters or {"type": "object", "properties": {}},
            }
            for t in tools
        ]

    @staticmethod
    def _messages_to_anthropic(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert harness Messages into Anthropic's `messages` list.

        Assistant messages carrying tool_calls become an assistant turn with
        `tool_use` blocks; a subsequent tool-role message's tool_results
        become a user turn with `tool_result` blocks. Anthropic has no
        "tool" role, so both map onto user/assistant content blocks.
        """
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                # System content is handled separately via the `system` param.
                continue
            if m.role == "tool":
                content: list[dict[str, Any]] = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tr.call_id,
                        "content": tr.content,
                        "is_error": tr.is_error,
                    }
                    for tr in m.tool_results
                ]
                if content:
                    out.append({"role": "user", "content": content})
                elif m.content:
                    out.append({"role": "user", "content": m.content})
                continue
            if m.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                if not blocks:
                    blocks.append({"type": "text", "text": ""})
                out.append({"role": "assistant", "content": blocks})
                continue
            # user
            out.append({"role": "user", "content": m.content})
        return out

    # -- response parsing ---------------------------------------------------
    @staticmethod
    def _parse_response(resp: Any) -> CompletionResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
                )
        stop_reason = "end"
        if resp.stop_reason == "tool_use":
            stop_reason = "tool_use"
        elif resp.stop_reason == "max_tokens":
            stop_reason = "max_tokens"
        usage = Usage(
            input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
        )
        return CompletionResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    # -- public API -----------------------------------------------------
    def complete(self, req: CompletionRequest) -> CompletionResponse:
        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens or _DEFAULT_MAX_TOKENS,
            "messages": self._messages_to_anthropic(req.messages),
        }
        if req.system:
            kwargs["system"] = req.system
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if req.tools:
            kwargs["tools"] = self._tool_specs_to_anthropic(req.tools)

        try:
            resp = self.client.messages.create(**kwargs)
        except self._anthropic.AuthenticationError as e:
            raise ProviderError(
                "Anthropic rejected the API key (authentication error). Check "
                "ANTHROPIC_API_KEY."
            ) from e
        except self._anthropic.RateLimitError as e:
            raise ProviderError(
                "Anthropic rate limit hit. Wait and retry, or reduce request rate."
            ) from e
        except self._anthropic.APIConnectionError as e:
            raise ProviderError(
                f"Could not connect to the Anthropic API: {e}. Check network/base_url."
            ) from e
        except self._anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error ({e.status_code}): {e.message}") from e

        return self._parse_response(resp)
