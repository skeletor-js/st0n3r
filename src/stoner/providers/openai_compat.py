"""OpenAI-compatible chat-completions provider.

One class covers OpenAI itself and any endpoint that speaks the same
`chat.completions` wire format via a different `base_url`: OpenRouter,
Together, Groq, Ollama, vLLM, LM Studio, etc.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import ProviderConfig, resolve_api_key
from ..types import CompletionRequest, CompletionResponse, Message, ToolCall, Usage
from .base import Provider, ProviderError

_DUMMY_LOCAL_KEY = "not-needed"


class OpenAICompatProvider(Provider):
    """Provider backed by the `openai` SDK, pointed at any compatible base_url."""

    supports_tools = True

    def __init__(self, name: str, pc: ProviderConfig):
        self.name = name
        self.pc = pc
        api_key = resolve_api_key(pc, ["OPENAI_API_KEY"])
        if not api_key:
            if pc.base_url:
                # Local/self-hosted endpoints (Ollama, vLLM, LM Studio...) typically
                # don't require a real key; the SDK still insists on a non-empty one.
                api_key = _DUMMY_LOCAL_KEY
            else:
                raise ProviderError(
                    "No OpenAI-compatible API key found. Set OPENAI_API_KEY "
                    f"{f'(or {pc.api_key_env}) ' if pc.api_key_env else ''}"
                    "in your environment, or add `api_key_env:` under this provider "
                    "in stoner.yaml."
                )
        try:
            import openai
        except ImportError as e:  # pragma: no cover - packaging issue, not test path
            raise ProviderError(
                "The `openai` package is not installed. Install it with "
                "`pip install st0n3r[openai]` or `pip install openai`."
            ) from e
        self._openai = openai
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if pc.base_url:
            client_kwargs["base_url"] = pc.base_url
        self.client = openai.OpenAI(**client_kwargs)

    # -- request building -------------------------------------------------
    @staticmethod
    def _tool_specs_to_openai(tools: list) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]

    @staticmethod
    def _messages_to_openai(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "system":
                out.append({"role": "system", "content": m.content})
            elif m.role == "tool":
                # One harness Message can carry several tool results; OpenAI wants
                # one `tool` message per call id.
                for tr in m.tool_results:
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": tr.call_id,
                            "content": tr.content,
                        }
                    )
            elif m.role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in m.tool_calls
                    ]
                out.append(msg)
            else:  # user
                out.append({"role": "user", "content": m.content})
        return out

    # -- response parsing -------------------------------------------------
    @staticmethod
    def _parse_tool_calls(raw_calls: list) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for rc in raw_calls or []:
            fn = rc.function
            raw_args = fn.arguments or "{}"
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
                if not isinstance(args, dict):
                    args = {"_note": "arguments were not a JSON object", "_raw": raw_args}
            except (json.JSONDecodeError, ValueError):
                args = {"_note": "failed to parse arguments as JSON", "_raw": raw_args}
            calls.append(ToolCall(id=rc.id, name=fn.name, arguments=args))
        return calls

    def _parse_response(self, resp: Any) -> CompletionResponse:
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = self._parse_tool_calls(getattr(msg, "tool_calls", None))
        finish = choice.finish_reason
        if finish == "tool_calls":
            stop_reason = "tool_use"
        elif finish == "length":
            stop_reason = "max_tokens"
        elif finish in ("stop", None):
            stop_reason = "end"
        else:
            stop_reason = "end"
        usage = Usage()
        if getattr(resp, "usage", None) is not None:
            usage = Usage(
                input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
            )
        return CompletionResponse(
            text=msg.content or "",
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    # -- public API -----------------------------------------------------
    def complete(self, req: CompletionRequest) -> CompletionResponse:
        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": self._messages_to_openai(req.system, req.messages),
            "max_tokens": req.max_tokens,
        }
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if req.tools:
            kwargs["tools"] = self._tool_specs_to_openai(req.tools)

        try:
            resp = self.client.chat.completions.create(**kwargs)
        except self._openai.AuthenticationError as e:
            raise ProviderError(
                f"{self.name}: API key was rejected (authentication error). Check "
                "OPENAI_API_KEY (or the provider's configured api_key_env)."
            ) from e
        except self._openai.RateLimitError as e:
            raise ProviderError(f"{self.name}: rate limit hit. Wait and retry.") from e
        except self._openai.APIConnectionError as e:
            raise ProviderError(
                f"{self.name}: could not connect to {self.pc.base_url or 'the API'}: {e}"
            ) from e
        except self._openai.APIStatusError as e:
            raise ProviderError(f"{self.name}: API error ({e.status_code}): {e.message}") from e

        return self._parse_response(resp)
