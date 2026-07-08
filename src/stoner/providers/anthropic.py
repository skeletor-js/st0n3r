"""Anthropic Messages API provider.

Maps the harness's provider-agnostic `Message`/`ToolSpec`/`ToolCall`/
`ToolResult` shapes onto Anthropic's content-block based Messages API and
back into a `CompletionResponse`.
"""

from __future__ import annotations

from typing import Any

from ..config import ProviderConfig, resolve_api_key
from ..types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    ToolCall,
    Usage,
    WebSearchSpec,
)
from .base import Provider, ProviderError

_DEFAULT_MAX_TOKENS = 8192

# When the server-side web-search tool runs, the API may return
# `stop_reason: "pause_turn"` mid-search; we resend the paused assistant turn
# to let it finish, bounded so a stuck server loop can never hang the harness.
_MAX_PAUSE_TURNS = 4


class AnthropicProvider(Provider):
    """Provider backed by the `anthropic` SDK (Claude models)."""

    name = "anthropic"
    supports_tools = True
    supports_web_search = True

    def __init__(self, pc: ProviderConfig):
        self.pc = pc
        api_key = resolve_api_key(pc, ["ANTHROPIC_API_KEY"])
        if not api_key:
            alt = pc.api_key_env if pc.api_key_env not in ("", "ANTHROPIC_API_KEY") else ""
            raise ProviderError(
                "No Anthropic API key found. Set ANTHROPIC_API_KEY "
                f"{f'(or {alt}) ' if alt else ''}"
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

    @staticmethod
    def _web_search_tool_dict(spec: WebSearchSpec) -> dict[str, Any]:
        """The verified `web_search_20250305` server-tool payload shape."""
        tool: dict[str, Any] = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": spec.max_uses,
        }
        if spec.allowed_domains:
            tool["allowed_domains"] = list(spec.allowed_domains)
        return tool

    @staticmethod
    def _content_to_input(content: Any) -> list[dict[str, Any]]:
        """Reconstruct content blocks as input dicts for a pause_turn resend.

        Server tool blocks (`server_tool_use`, `web_search_tool_result`)
        carry `encrypted_content` that must be passed back verbatim; pydantic
        SDK blocks expose `model_dump()`. Blocks we can't reconstruct are
        dropped -- harmless, since the resend only needs the searchable state.
        """
        out: list[dict[str, Any]] = []
        for block in content or []:
            if hasattr(block, "model_dump"):
                out.append(block.model_dump())
                continue
            btype = getattr(block, "type", None)
            if btype == "text":
                out.append({"type": "text", "text": block.text})
            elif btype == "tool_use":
                out.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": dict(block.input or {}),
                    }
                )
        return out

    @staticmethod
    def _extract_web(resp: Any) -> tuple[int, list[str], list[str]]:
        """Pull (search count, queries, cited URLs) from a response.

        Count comes from `usage.server_tool_use.web_search_requests`; queries
        from `server_tool_use` input blocks; URLs from `web_search_tool_result`
        result items. All best-effort -- used for the ledger trail only.
        """
        web = 0
        usage = getattr(resp, "usage", None)
        stu = getattr(usage, "server_tool_use", None)
        if stu is not None:
            web = getattr(stu, "web_search_requests", 0) or 0
        queries: list[str] = []
        urls: list[str] = []
        for block in getattr(resp, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "server_tool_use":
                inp = getattr(block, "input", None)
                if isinstance(inp, dict) and inp.get("query"):
                    queries.append(str(inp["query"]))
            elif btype == "web_search_tool_result":
                for item in getattr(block, "content", []) or []:
                    url = getattr(item, "url", None)
                    if url is None and isinstance(item, dict):
                        url = item.get("url")
                    if url:
                        urls.append(str(url))
        return web, queries, urls

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

    def _create(self, kwargs: dict[str, Any]) -> Any:
        """One `messages.create` call with vendor errors wrapped."""
        try:
            return self.client.messages.create(**kwargs)
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
        tools = self._tool_specs_to_anthropic(req.tools) if req.tools else []
        if req.web_search is not None:
            tools.append(self._web_search_tool_dict(req.web_search))
        if tools:
            kwargs["tools"] = tools

        # Server-side web search may pause and resume mid-turn; loop over
        # `pause_turn` up to a bound, resending the paused assistant content.
        messages = list(kwargs["messages"])
        total_web = 0
        all_queries: list[str] = []
        all_urls: list[str] = []
        resp: Any = None
        for _ in range(_MAX_PAUSE_TURNS + 1):
            resp = self._create(kwargs)
            web, queries, urls = self._extract_web(resp)
            total_web += web
            all_queries.extend(queries)
            all_urls.extend(urls)
            if getattr(resp, "stop_reason", None) == "pause_turn":
                messages = messages + [
                    {"role": "assistant", "content": self._content_to_input(resp.content)}
                ]
                kwargs["messages"] = messages
                continue
            break

        parsed = self._parse_response(resp)
        parsed.usage.web_searches = total_web
        if req.web_search is not None and parsed.raw is not None:
            parsed.raw["web_search_queries"] = all_queries
            parsed.raw["web_search_urls"] = all_urls
        return parsed
