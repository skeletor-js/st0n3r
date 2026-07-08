"""Tests for the web-capable completion seam (U1).

No network: the Anthropic SDK client is monkeypatched with fake objects that
mimic just enough of the Messages response shape (including the server
web-search block types and pause_turn continuation) to exercise our request
building and response parsing. The claude CLI is driven through a
subprocess stub, mirroring tests/test_claude_code_provider.py.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from stoner.config import ProviderConfig
from stoner.providers.base import ProviderError
from stoner.types import (
    CompletionRequest,
    Message,
    Usage,
    WebSearchSpec,
)

# ---------------------------------------------------------------------------
# Fake Anthropic SDK shapes
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, inp=10, out=20, web_search_requests=None):
        self.input_tokens = inp
        self.output_tokens = out
        if web_search_requests is not None:
            self.server_tool_use = _FakeServerToolUse(web_search_requests)
        else:
            self.server_tool_use = None


class _FakeServerToolUse:
    def __init__(self, web_search_requests):
        self.web_search_requests = web_search_requests


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _ServerToolUseBlock:
    type = "server_tool_use"

    def __init__(self, query):
        self.input = {"query": query}


class _WebSearchResultItem:
    def __init__(self, url):
        self.url = url


class _WebSearchToolResultBlock:
    type = "web_search_tool_result"

    def __init__(self, urls):
        self.content = [_WebSearchResultItem(u) for u in urls]


class _FakeResponse:
    def __init__(self, content, stop_reason="end_turn", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _FakeUsage()

    def model_dump(self):
        return {"stop_reason": self.stop_reason}


class _FakeMessages:
    """Returns a queued list of responses, one per create() call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from stoner.providers.anthropic import AnthropicProvider

    return AnthropicProvider(ProviderConfig(kind="anthropic"))


# ---------------------------------------------------------------------------
# Anthropic mapping
# ---------------------------------------------------------------------------


def test_anthropic_web_search_tool_dict_in_request(monkeypatch):
    provider = _anthropic(monkeypatch)
    fake = _FakeMessages([_FakeResponse([_TextBlock("done")])])
    provider.client.messages = fake

    req = CompletionRequest(
        model="claude-sonnet-5",
        messages=[Message(role="user", content="research the fee")],
        web_search=WebSearchSpec(max_uses=6, allowed_domains=["humboldtgov.org"]),
    )
    provider.complete(req)

    tools = fake.calls[0]["tools"]
    ws = next(t for t in tools if t.get("type") == "web_search_20250305")
    assert ws["name"] == "web_search"
    assert ws["max_uses"] == 6
    assert ws["allowed_domains"] == ["humboldtgov.org"]


def test_anthropic_parses_server_tool_blocks_and_counts(monkeypatch):
    provider = _anthropic(monkeypatch)
    resp = _FakeResponse(
        [
            _ServerToolUseBlock("humboldt reinspection fee"),
            _WebSearchToolResultBlock(["https://humboldtgov.org/code"]),
            _TextBlock("The fee is $1,200."),
        ],
        usage=_FakeUsage(web_search_requests=2),
    )
    provider.client.messages = _FakeMessages([resp])

    out = provider.complete(
        CompletionRequest(
            model="m",
            messages=[Message(role="user", content="hi")],
            web_search=WebSearchSpec(),
        )
    )

    assert out.text == "The fee is $1,200."
    assert out.usage.web_searches == 2
    assert out.raw is not None
    assert out.raw["web_search_queries"] == ["humboldt reinspection fee"]
    assert out.raw["web_search_urls"] == ["https://humboldtgov.org/code"]


def test_anthropic_pause_turn_triggers_bounded_continuation(monkeypatch):
    provider = _anthropic(monkeypatch)
    paused = _FakeResponse(
        [_ServerToolUseBlock("query one")],
        stop_reason="pause_turn",
        usage=_FakeUsage(web_search_requests=1),
    )
    final = _FakeResponse(
        [_TextBlock("final text")],
        stop_reason="end_turn",
        usage=_FakeUsage(web_search_requests=1),
    )
    fake = _FakeMessages([paused, final])
    provider.client.messages = fake

    out = provider.complete(
        CompletionRequest(
            model="m",
            messages=[Message(role="user", content="hi")],
            web_search=WebSearchSpec(),
        )
    )

    assert out.text == "final text"
    # Two create() calls: the paused turn plus one continuation.
    assert len(fake.calls) == 2
    # The continuation resent the paused assistant content.
    resent = fake.calls[1]["messages"]
    assert resent[-1]["role"] == "assistant"
    # Web-search counts accumulate across the paused and final turns.
    assert out.usage.web_searches == 2


def test_anthropic_no_spec_is_unchanged(monkeypatch):
    """A request without a web_search spec adds no tools key."""
    provider = _anthropic(monkeypatch)
    fake = _FakeMessages([_FakeResponse([_TextBlock("hi")])])
    provider.client.messages = fake

    provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="x")]))
    assert "tools" not in fake.calls[0]


# ---------------------------------------------------------------------------
# claude CLI mapping
# ---------------------------------------------------------------------------

_FULL_HELP = """Usage: claude [options] [prompt]
  --model <model>            Model for the current session.
  --output-format <format>   Output format ("text", "json").
  --tools <tools...>         Available tools. Use "" to disable all tools.
  --no-session-persistence   Disable session persistence.
  -p, --print                Print response and exit.
"""

_HELP_NO_TOOLS = """Usage: claude [options] [prompt]
  --model <model>   Model for the current session.
  -p, --print       Print response and exit.
"""


class _Result:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_cli(monkeypatch, calls, help_text=_FULL_HELP):
    from stoner.providers import claude_code

    claude_code._HELP_CACHE.clear()

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        calls.append(cmd)
        if cmd[1:3] == ["-p", "--help"]:
            return _Result(help_text)
        return _Result("web-informed reply")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude")
    return claude_code


def test_claude_code_enables_web_tools_when_spec_set(monkeypatch):
    calls: list[list[str]] = []
    claude_code = _patch_cli(monkeypatch, calls)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    out = provider.complete(
        CompletionRequest(
            model="m",
            messages=[Message(role="user", content="hi")],
            web_search=WebSearchSpec(),
        )
    )

    run_call = calls[-1]
    assert "--tools" in run_call
    assert run_call[run_call.index("--tools") + 1] == "WebSearch,WebFetch"
    assert out.raw is not None and "web_search_note" in out.raw


def test_claude_code_disables_tools_when_no_spec(monkeypatch):
    calls: list[list[str]] = []
    claude_code = _patch_cli(monkeypatch, calls)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="hi")]))

    run_call = calls[-1]
    assert run_call[run_call.index("--tools") + 1] == ""


def test_claude_code_web_search_without_tools_flag_raises(monkeypatch):
    calls: list[list[str]] = []
    claude_code = _patch_cli(monkeypatch, calls, help_text=_HELP_NO_TOOLS)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    with pytest.raises(ProviderError, match="--tools"):
        provider.complete(
            CompletionRequest(
                model="m",
                messages=[Message(role="user", content="hi")],
                web_search=WebSearchSpec(),
            )
        )


# ---------------------------------------------------------------------------
# Refusal on providers without support + Usage math
# ---------------------------------------------------------------------------


def test_openai_compat_raises_when_spec_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from stoner.providers.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider("openai", ProviderConfig(kind="openai_compat"))
    assert provider.supports_web_search is False
    with pytest.raises(ProviderError, match="web search"):
        provider.complete(
            CompletionRequest(
                model="m",
                messages=[Message(role="user", content="hi")],
                web_search=WebSearchSpec(),
            )
        )


def test_codex_cli_raises_when_spec_set(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex")
    from stoner.providers import codex_cli

    codex_cli._HELP_CACHE.clear()
    provider = codex_cli.CodexCLIProvider(ProviderConfig(kind="codex_cli"))
    assert provider.supports_web_search is False
    with pytest.raises(ProviderError, match="web search"):
        provider.complete(
            CompletionRequest(
                model="m",
                messages=[Message(role="user", content="hi")],
                web_search=WebSearchSpec(),
            )
        )


def test_usage_addition_sums_web_searches():
    total = Usage(input_tokens=1, output_tokens=2, web_searches=3) + Usage(
        input_tokens=4, output_tokens=5, web_searches=6
    )
    assert total.web_searches == 9
    assert total.input_tokens == 5
    assert total.output_tokens == 7
