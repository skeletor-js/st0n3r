"""Tests for the `claude` CLI backed provider.

No real CLI calls: `shutil.which` and `subprocess.run` are monkeypatched.
One opt-in live smoke test (skipped by default) exercises the real `claude`
binary when STONER_LIVE_TESTS is set and the binary is on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from stoner.config import ProviderConfig
from stoner.providers.base import ProviderError
from stoner.types import CompletionRequest, Message


class _Result:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


_FULL_HELP = """Usage: claude [options] [prompt]

  --model <model>            Model for the current session.
  --output-format <format>   Output format (only works with --print):
                              "text" (default), "json" (single result), or
                              "stream-json" (choices: "text", "json",
                              "stream-json")
  --tools <tools...>         Specify the list of available tools. Use "" to
                              disable all tools.
  --no-session-persistence   Disable session persistence.
  -p, --print                Print response and exit.
"""


def _patch_help(monkeypatch, calls, help_text=_FULL_HELP, run_result=None):
    from stoner.providers import claude_code

    claude_code._HELP_CACHE.clear()

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        calls.append(cmd)
        if cmd[1:3] == ["-p", "--help"]:
            return _Result(help_text)
        if run_result is not None:
            return run_result(cmd)
        return _Result("plain text reply")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude")
    return claude_code


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------


def test_claude_code_missing_binary_raises(monkeypatch):
    from stoner.providers import claude_code

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ProviderError, match="claude"):
        claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))


# ---------------------------------------------------------------------------
# Prompt composition / argv construction
# ---------------------------------------------------------------------------


def test_claude_code_prompt_composition_and_argv(monkeypatch):
    calls: list[list[str]] = []
    claude_code = _patch_help(monkeypatch, calls)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    req = CompletionRequest(
        model="claude-haiku-4-5-20251001",
        system="be terse",
        messages=[Message(role="user", content="write something")],
    )
    resp = provider.complete(req)

    assert resp.text == "plain text reply"
    assert resp.stop_reason == "end"

    run_call = calls[-1]
    assert run_call[0] == "/usr/bin/claude"
    assert run_call[1] == "-p"
    assert "[SYSTEM]" in run_call[2]
    assert "be terse" in run_call[2]
    assert "[USER]" in run_call[2]
    assert "write something" in run_call[2]
    assert "--model" in run_call
    assert run_call[run_call.index("--model") + 1] == "claude-haiku-4-5-20251001"


def test_claude_code_disables_tools_and_session_persistence(monkeypatch):
    calls: list[list[str]] = []
    claude_code = _patch_help(monkeypatch, calls)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="hi")]))

    run_call = calls[-1]
    assert "--tools" in run_call
    assert run_call[run_call.index("--tools") + 1] == ""
    assert "--no-session-persistence" in run_call


def test_claude_code_omits_model_flag_when_empty(monkeypatch):
    calls: list[list[str]] = []
    claude_code = _patch_help(monkeypatch, calls)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    provider.complete(CompletionRequest(model="", messages=[Message(role="user", content="hi")]))

    run_call = calls[-1]
    assert "--model" not in run_call


def test_claude_code_runs_in_scratch_cwd(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    cwds: list[str] = []

    from stoner.providers import claude_code as cc_mod

    cc_mod._HELP_CACHE.clear()

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        cwds.append(cwd)
        calls.append(cmd)
        if cmd[1:3] == ["-p", "--help"]:
            return _Result(_FULL_HELP)
        return _Result("plain text reply")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude")
    provider = cc_mod.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="hi")]))

    # help call has no cwd captured meaningfully here; the actual complete()
    # call's cwd must be a scratch dir, not the repo/test cwd.
    real_cwd = os.getcwd()
    assert cwds[-1] is not None
    assert cwds[-1] != real_cwd
    assert "stoner-claude-" in cwds[-1]


# ---------------------------------------------------------------------------
# Timeout / errors
# ---------------------------------------------------------------------------


def test_claude_code_timeout_raises_provider_error(monkeypatch):
    from stoner.providers import claude_code

    claude_code._HELP_CACHE.clear()

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        if cmd[1:3] == ["-p", "--help"]:
            return _Result(_FULL_HELP)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude")

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    with pytest.raises(ProviderError, match="timed out"):
        provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="hi")]))


def test_claude_code_nonzero_exit_raises_with_stderr(monkeypatch):
    calls: list[list[str]] = []

    def run_result(cmd):
        return _Result("", returncode=1, stderr="auth failed: not logged in")

    claude_code = _patch_help(monkeypatch, calls, run_result=run_result)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    with pytest.raises(ProviderError, match="auth failed"):
        provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="hi")]))


def test_claude_code_stderr_capped_at_500_chars(monkeypatch):
    calls: list[list[str]] = []
    long_stderr = "x" * 2000

    def run_result(cmd):
        return _Result("", returncode=1, stderr=long_stderr)

    claude_code = _patch_help(monkeypatch, calls, run_result=run_result)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    with pytest.raises(ProviderError) as exc_info:
        provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="hi")]))
    assert len(str(exc_info.value)) < 600


# ---------------------------------------------------------------------------
# Output parsing: JSON vs plain text
# ---------------------------------------------------------------------------


def test_claude_code_parses_json_output_when_supported(monkeypatch):
    calls: list[list[str]] = []
    payload = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "the final reply",
            "usage": {"input_tokens": 12, "output_tokens": 34},
        }
    )

    def run_result(cmd):
        return _Result(payload)

    claude_code = _patch_help(monkeypatch, calls, run_result=run_result)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    resp = provider.complete(
        CompletionRequest(model="m", messages=[Message(role="user", content="hi")])
    )

    assert resp.text == "the final reply"
    assert resp.usage.input_tokens == 12
    assert resp.usage.output_tokens == 34
    run_call = calls[-1]
    assert "--output-format" in run_call
    assert run_call[run_call.index("--output-format") + 1] == "json"


def test_claude_code_json_error_result_raises(monkeypatch):
    calls: list[list[str]] = []
    payload = json.dumps(
        {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": "budget exceeded",
            "usage": {},
        }
    )

    def run_result(cmd):
        return _Result(payload)

    claude_code = _patch_help(monkeypatch, calls, run_result=run_result)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    with pytest.raises(ProviderError, match="budget exceeded"):
        provider.complete(CompletionRequest(model="m", messages=[Message(role="user", content="hi")]))


def test_claude_code_plain_text_fallback_when_json_unsupported(monkeypatch):
    calls: list[list[str]] = []
    help_no_json = """Usage: claude [options] [prompt]
  --model <model>   Model for the current session.
  -p, --print       Print response and exit.
"""

    claude_code = _patch_help(monkeypatch, calls, help_text=help_no_json)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    resp = provider.complete(
        CompletionRequest(model="m", messages=[Message(role="user", content="hi")])
    )

    assert resp.text == "plain text reply"
    run_call = calls[-1]
    assert "--output-format" not in run_call
    assert "--tools" not in run_call
    assert "--no-session-persistence" not in run_call


def test_claude_code_malformed_json_falls_back_to_raw_stdout(monkeypatch):
    calls: list[list[str]] = []

    def run_result(cmd):
        return _Result("not actually json {{{")

    claude_code = _patch_help(monkeypatch, calls, run_result=run_result)

    provider = claude_code.ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    resp = provider.complete(
        CompletionRequest(model="m", messages=[Message(role="user", content="hi")])
    )
    assert resp.text == "not actually json {{{"
    assert resp.usage.input_tokens == 0


# ---------------------------------------------------------------------------
# Live smoke test (opt-in, real CLI)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not shutil.which("claude") or not os.environ.get("STONER_LIVE_TESTS"),
    reason="requires the real `claude` CLI on PATH and STONER_LIVE_TESTS=1",
)
def test_claude_code_live_smoke():
    from stoner.providers.claude_code import ClaudeCodeProvider

    provider = ClaudeCodeProvider(ProviderConfig(kind="claude_code"))
    resp = provider.complete(
        CompletionRequest(
            model="claude-haiku-4-5-20251001",
            system="Reply with exactly the text: PONG",
            messages=[Message(role="user", content="ping")],
            max_tokens=32,
        )
    )
    assert "PONG" in resp.text
