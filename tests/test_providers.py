"""Tests for the vendor-backed providers.

No network calls: SDK clients are monkeypatched with fake objects that mimic
just enough of the anthropic/openai response shapes to exercise our request-
building and response-parsing code.
"""

from __future__ import annotations

import json

import httpx
import pytest

from stoner.config import ProviderConfig
from stoner.providers.base import ProviderError
from stoner.types import (
    CompletionRequest,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from stoner.providers.anthropic import AnthropicProvider

    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(ProviderConfig(kind="anthropic"))


class _FakeAnthropicUsage:
    def __init__(self, inp=10, out=20):
        self.input_tokens = inp
        self.output_tokens = out


class _FakeAnthropicTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeAnthropicToolUseBlock:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class _FakeAnthropicResponse:
    def __init__(self, content, stop_reason="end_turn", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _FakeAnthropicUsage()


class _FakeAnthropicMessages:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


def _make_anthropic_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from stoner.providers.anthropic import AnthropicProvider

    return AnthropicProvider(ProviderConfig(kind="anthropic"))


def test_anthropic_request_building_and_text_response(monkeypatch):
    provider = _make_anthropic_provider(monkeypatch)
    fake_messages = _FakeAnthropicMessages(
        response=_FakeAnthropicResponse([_FakeAnthropicTextBlock("hello there")])
    )
    provider.client.messages = fake_messages

    req = CompletionRequest(
        model="claude-sonnet-5",
        system="be nice",
        messages=[Message(role="user", content="hi")],
        tools=[ToolSpec(name="ping", description="pings", parameters={"type": "object"})],
        max_tokens=512,
        temperature=0.7,
    )
    resp = provider.complete(req)

    assert resp.text == "hello there"
    assert resp.tool_calls == []
    assert resp.stop_reason == "end"
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 20

    kwargs = fake_messages.last_kwargs
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["system"] == "be nice"
    assert kwargs["max_tokens"] == 512
    assert kwargs["temperature"] == 0.7
    assert kwargs["tools"][0]["name"] == "ping"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_tool_use_response_and_roundtrip(monkeypatch):
    provider = _make_anthropic_provider(monkeypatch)
    fake_messages = _FakeAnthropicMessages(
        response=_FakeAnthropicResponse(
            [
                _FakeAnthropicTextBlock("let me check"),
                _FakeAnthropicToolUseBlock("toolu_1", "read_chapter", {"number": 1}),
            ],
            stop_reason="tool_use",
        )
    )
    provider.client.messages = fake_messages

    # Simulate a follow-up turn: assistant message with a tool call, then a
    # tool-role message carrying the result, both need correct block mapping.
    messages = [
        Message(role="user", content="write chapter 1"),
        Message(
            role="assistant",
            content="let me check",
            tool_calls=[ToolCall(id="toolu_1", name="read_chapter", arguments={"number": 1})],
        ),
        Message(
            role="tool",
            tool_results=[
                ToolResult(call_id="toolu_1", name="read_chapter", content="chapter text")
            ],
        ),
    ]
    req = CompletionRequest(model="claude-sonnet-5", messages=messages)
    resp = provider.complete(req)

    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].name == "read_chapter"
    assert resp.tool_calls[0].arguments == {"number": 1}

    sent = fake_messages.last_kwargs["messages"]
    assert sent[1]["role"] == "assistant"
    assert sent[1]["content"][1]["type"] == "tool_use"
    assert sent[1]["content"][1]["id"] == "toolu_1"
    assert sent[2]["role"] == "user"
    assert sent[2]["content"][0]["type"] == "tool_result"
    assert sent[2]["content"][0]["tool_use_id"] == "toolu_1"


def test_anthropic_auth_error_wrapped(monkeypatch):
    import anthropic as anthropic_sdk

    provider = _make_anthropic_provider(monkeypatch)
    req_obj = httpx.Request("POST", "http://x/v1/messages")
    resp_obj = httpx.Response(401, request=req_obj)
    exc = anthropic_sdk.AuthenticationError("bad key", response=resp_obj, body=None)
    provider.client.messages = _FakeAnthropicMessages(exc=exc)

    with pytest.raises(ProviderError, match="authentication"):
        provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="hi")]))


def test_anthropic_connection_error_wrapped(monkeypatch):
    import anthropic as anthropic_sdk

    provider = _make_anthropic_provider(monkeypatch)
    req_obj = httpx.Request("POST", "http://x/v1/messages")
    exc = anthropic_sdk.APIConnectionError(message="conn fail", request=req_obj)
    provider.client.messages = _FakeAnthropicMessages(exc=exc)

    with pytest.raises(ProviderError, match="connect"):
        provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="hi")]))


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------


def test_openai_compat_missing_key_no_base_url_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from stoner.providers.openai_compat import OpenAICompatProvider

    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        OpenAICompatProvider("openai", ProviderConfig(kind="openai_compat"))


def test_openai_compat_local_endpoint_uses_dummy_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from stoner.providers.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider(
        "ollama",
        ProviderConfig(kind="openai_compat", base_url="http://localhost:11434/v1"),
    )
    assert provider.client.api_key == "not-needed"


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _FakeFunction(name, arguments)


class _FakeOpenAIMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeOpenAIUsage:
    def __init__(self, prompt_tokens=5, completion_tokens=7):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeOpenAIResponse:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage or _FakeOpenAIUsage()


class _FakeCompletions:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


def _make_openai_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from stoner.providers.openai_compat import OpenAICompatProvider

    return OpenAICompatProvider("openai", ProviderConfig(kind="openai_compat"))


def test_openai_compat_request_building_and_text_response(monkeypatch):
    provider = _make_openai_provider(monkeypatch)
    completions = _FakeCompletions(
        response=_FakeOpenAIResponse([_FakeChoice(_FakeOpenAIMessage(content="hi there"))])
    )
    provider.client.chat = _FakeChat(completions)

    req = CompletionRequest(
        model="gpt-5.2",
        system="be nice",
        messages=[Message(role="user", content="hello")],
        tools=[ToolSpec(name="ping", description="pings", parameters={"type": "object"})],
        max_tokens=256,
    )
    resp = provider.complete(req)

    assert resp.text == "hi there"
    assert resp.stop_reason == "end"
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 7

    kwargs = completions.last_kwargs
    assert kwargs["messages"][0] == {"role": "system", "content": "be nice"}
    assert kwargs["messages"][1] == {"role": "user", "content": "hello"}
    assert kwargs["tools"][0]["function"]["name"] == "ping"


def test_openai_compat_tool_call_roundtrip(monkeypatch):
    provider = _make_openai_provider(monkeypatch)
    tool_calls = [_FakeToolCall("call_1", "read_chapter", json.dumps({"number": 2}))]
    completions = _FakeCompletions(
        response=_FakeOpenAIResponse(
            [_FakeChoice(_FakeOpenAIMessage(content="", tool_calls=tool_calls), finish_reason="tool_calls")]
        )
    )
    provider.client.chat = _FakeChat(completions)

    messages = [
        Message(role="user", content="write ch 2"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="call_1", name="read_chapter", arguments={"number": 2})],
        ),
        Message(
            role="tool",
            tool_results=[ToolResult(call_id="call_1", name="read_chapter", content="text")],
        ),
    ]
    resp = provider.complete(CompletionRequest(model="gpt-5.2", messages=messages))

    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].name == "read_chapter"
    assert resp.tool_calls[0].arguments == {"number": 2}

    sent = completions.last_kwargs["messages"]
    assistant_sent = next(m for m in sent if m.get("role") == "assistant" and m.get("tool_calls"))
    assert assistant_sent["tool_calls"][0]["function"]["name"] == "read_chapter"
    tool_sent = next(m for m in sent if m.get("role") == "tool")
    assert tool_sent["tool_call_id"] == "call_1"
    assert tool_sent["content"] == "text"


def test_openai_compat_malformed_tool_arguments(monkeypatch):
    provider = _make_openai_provider(monkeypatch)
    tool_calls = [_FakeToolCall("call_1", "read_chapter", "{not json")]
    completions = _FakeCompletions(
        response=_FakeOpenAIResponse(
            [_FakeChoice(_FakeOpenAIMessage(content="", tool_calls=tool_calls), finish_reason="tool_calls")]
        )
    )
    provider.client.chat = _FakeChat(completions)

    resp = provider.complete(
        CompletionRequest(model="gpt-5.2", messages=[Message(role="user", content="hi")])
    )

    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert call.name == "read_chapter"
    assert "_note" in call.arguments  # empty/annotated args rather than a raised exception


def test_openai_compat_auth_error_wrapped(monkeypatch):
    import openai as openai_sdk

    provider = _make_openai_provider(monkeypatch)
    req_obj = httpx.Request("POST", "http://x/v1/chat/completions")
    resp_obj = httpx.Response(401, request=req_obj)
    exc = openai_sdk.AuthenticationError("bad key", response=resp_obj, body=None)
    provider.client.chat = _FakeChat(_FakeCompletions(exc=exc))

    with pytest.raises(ProviderError, match="rejected"):
        provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="hi")]))


# ---------------------------------------------------------------------------
# codex_cli
# ---------------------------------------------------------------------------


def test_codex_cli_missing_binary_raises(monkeypatch):
    import shutil

    from stoner.providers import codex_cli

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ProviderError, match="codex"):
        codex_cli.CodexCLIProvider(ProviderConfig(kind="codex_cli"))


def test_codex_cli_builds_prompt_and_runs(monkeypatch, tmp_path):
    import shutil
    import subprocess

    from stoner.providers import codex_cli

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex")
    codex_cli._HELP_CACHE.clear()

    calls = []

    class _Result:
        def __init__(self, stdout, returncode=0, stderr=""):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def fake_run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        if cmd[1:3] == ["exec", "--help"]:
            return _Result("usage: codex exec [--json] ...")
        return _Result("plain text reply")

    monkeypatch.setattr(subprocess, "run", fake_run)

    provider = codex_cli.CodexCLIProvider(ProviderConfig(kind="codex_cli"))
    req = CompletionRequest(
        model="gpt-5-codex",
        system="be terse",
        messages=[Message(role="user", content="write something")],
    )
    resp = provider.complete(req)

    assert resp.text == "plain text reply"
    assert resp.stop_reason == "end"
    # the --json flag was detected and passed through
    exec_call = calls[-1]
    assert "--json" in exec_call
    assert any("[SYSTEM]" in part for part in exec_call)


def test_codex_cli_parses_jsonl_agent_message(monkeypatch):
    import shutil
    import subprocess

    from stoner.providers import codex_cli

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex")
    codex_cli._HELP_CACHE.clear()

    class _Result:
        def __init__(self, stdout, returncode=0, stderr=""):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    jsonl = "\n".join(
        [
            json.dumps({"type": "session.created"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final answer"}}),
        ]
    )

    def fake_run(cmd, capture_output, text, timeout):
        if cmd[1:3] == ["exec", "--help"]:
            return _Result("usage: codex exec [--json] ...")
        return _Result(jsonl)

    monkeypatch.setattr(subprocess, "run", fake_run)

    provider = codex_cli.CodexCLIProvider(ProviderConfig(kind="codex_cli"))
    resp = provider.complete(
        CompletionRequest(model="gpt-5-codex", messages=[Message(role="user", content="hi")])
    )
    assert resp.text == "final answer"


def test_codex_cli_nonzero_exit_raises(monkeypatch):
    import shutil
    import subprocess

    from stoner.providers import codex_cli

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex")
    codex_cli._HELP_CACHE.clear()

    class _Result:
        def __init__(self, stdout, returncode, stderr):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def fake_run(cmd, capture_output, text, timeout):
        if cmd[1:3] == ["exec", "--help"]:
            return _Result("usage: codex exec ...", 0, "")
        return _Result("", 1, "boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    provider = codex_cli.CodexCLIProvider(ProviderConfig(kind="codex_cli"))
    with pytest.raises(ProviderError, match="boom"):
        provider.complete(CompletionRequest(model="gpt-5-codex", messages=[Message(role="user", content="hi")]))
