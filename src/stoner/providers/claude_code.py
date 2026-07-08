"""Provider that shells out to the `claude` CLI (Claude Code, Claude-auth).

Claude Code CLI is driven here in non-interactive `-p`/`--print` mode as a
text-only backend (`supports_tools = False`); the engine degrades to the
fenced-JSON tool protocol for this provider. Like codex_cli.py, this adapter
feature-detects flags via `claude -p --help` (cached per process) so it keeps
working across CLI version drift, and it runs the subprocess in a scratch
temp directory with all built-in tools disabled so the CLI can never touch
the caller's working directory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile

from ..config import ProviderConfig
from ..types import CompletionRequest, CompletionResponse, Usage
from .base import Provider, ProviderError
from .codex_cli import CodexCLIProvider

_DEFAULT_TIMEOUT = 600

# Cached per-process: binary path -> `claude -p --help` output.
_HELP_CACHE: dict[str, str] = {}


class ClaudeCodeProvider(Provider):
    """Provider backed by shelling out to the `claude` CLI in print mode."""

    name = "claude_code"
    supports_tools = False
    # The `claude` CLI ships its own WebSearch/WebFetch tools; research calls
    # re-enable exactly those (feature-detected) while the filesystem jail
    # (scratch cwd, no other tools) stays intact. It cannot enforce a domain
    # allowlist and does not report a per-search count.
    supports_web_search = True

    def __init__(self, pc: ProviderConfig):
        self.pc = pc
        self.binary = shutil.which("claude")
        if not self.binary:
            raise ProviderError(
                "The `claude` CLI was not found on PATH. Install Claude Code "
                "(https://claude.com/claude-code) and sign in by running `claude` "
                "once interactively, or via `claude setup-token`, then retry."
            )
        self.timeout = pc.extra.get("timeout", _DEFAULT_TIMEOUT)

    # -- feature detection -------------------------------------------------
    def _help_text(self) -> str:
        if self.binary not in _HELP_CACHE:
            try:
                r = subprocess.run(
                    [self.binary, "-p", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                _HELP_CACHE[self.binary] = (r.stdout or "") + (r.stderr or "")
            except (subprocess.TimeoutExpired, OSError):
                _HELP_CACHE[self.binary] = ""
        return _HELP_CACHE[self.binary]

    # -- prompt assembly -----------------------------------------------------
    # Same role-labelled prompt composition as codex_cli; reused rather than
    # duplicated since both CLIs take a single opaque prompt string.
    _build_prompt = staticmethod(CodexCLIProvider._build_prompt)

    # -- output parsing -------------------------------------------------
    @staticmethod
    def _parse_json_result(stdout: str) -> tuple[str | None, Usage]:
        """Parse `claude -p --output-format json` output: a single JSON object
        with a `result` text field and an `usage` dict of token counts."""
        stdout = stdout.strip()
        if not stdout:
            return None, Usage()
        try:
            obj = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return None, Usage()
        if not isinstance(obj, dict):
            return None, Usage()
        if obj.get("is_error"):
            detail = str(obj.get("result") or obj.get("subtype") or "unknown error")
            raise ProviderError(f"`claude -p` reported an error: {detail[:500]}")
        text = obj.get("result")
        if not isinstance(text, str):
            return None, Usage()
        usage_obj = obj.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_obj.get("input_tokens", 0) or 0),
            output_tokens=int(usage_obj.get("output_tokens", 0) or 0),
        )
        return text, usage

    # -- public API -----------------------------------------------------
    def complete(self, req: CompletionRequest) -> CompletionResponse:
        prompt = self._build_prompt(req)
        help_text = self._help_text()
        use_json = "--output-format" in help_text and "json" in help_text
        supports_tools_flag = "--tools" in help_text
        supports_no_session = "--no-session-persistence" in help_text

        cmd = [self.binary, "-p", prompt]
        if req.model:
            cmd += ["--model", req.model]
        if use_json:
            cmd += ["--output-format", "json"]
        if req.web_search is not None:
            # Research call: re-enable exactly the web tools. Everything else
            # (filesystem, bash) stays off and the scratch cwd is unchanged.
            if not supports_tools_flag:
                raise ProviderError(
                    "This `claude` CLI build does not expose the `--tools` flag, so "
                    "web search cannot be enabled for it. Upgrade Claude Code, or use "
                    "an Anthropic API researcher role for `facts research`."
                )
            cmd += ["--tools", "WebSearch,WebFetch"]
        elif supports_tools_flag:
            # Disable every built-in tool: this is a text-only backend and
            # must never let the CLI touch the filesystem or run commands.
            cmd += ["--tools", ""]
        if supports_no_session:
            cmd.append("--no-session-persistence")

        scratch_dir = tempfile.mkdtemp(prefix="stoner-claude-")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=scratch_dir,
            )
        except subprocess.TimeoutExpired as e:
            raise ProviderError(
                f"`claude -p` timed out after {self.timeout}s. Increase "
                "`extra.timeout` on the claude provider config if the task is large."
            ) from e
        except OSError as e:
            raise ProviderError(f"Failed to run `claude` CLI: {e}") from e
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:500]
            raise ProviderError(f"`claude -p` failed (exit {result.returncode}): {detail}")

        text: str | None = None
        usage = Usage()
        if use_json:
            text, usage = self._parse_json_result(result.stdout)
        if text is None:
            text = result.stdout.strip()

        raw: dict[str, object] = {"stdout": result.stdout, "stderr": result.stderr}
        if req.web_search is not None:
            # The CLI does not surface a per-search count or honor a domain
            # allowlist; record that so the ledger trail stays honest.
            raw["web_search_note"] = (
                "claude CLI WebSearch/WebFetch enabled; per-search count "
                "unavailable and domain allowlist not enforced"
            )
        return CompletionResponse(
            text=text,
            tool_calls=[],
            stop_reason="end",
            usage=usage,
            raw=raw,
        )
