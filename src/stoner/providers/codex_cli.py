"""Provider that shells out to the `codex` CLI (OpenAI Codex, ChatGPT-auth).

Codex CLI is text-only in v1 (`supports_tools = False`); the engine degrades
to the fenced-JSON tool protocol for this provider. This adapter is
deliberately defensive about CLI version drift: it feature-detects flags via
`codex exec --help` (cached per process) and falls back to plain
`codex exec "<prompt>"` capture if structured output isn't available.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from ..config import ProviderConfig
from ..types import CompletionRequest, CompletionResponse, Usage
from .base import Provider, ProviderError

_DEFAULT_TIMEOUT = 600

# Cached per-process: binary path -> `codex exec --help` output.
_HELP_CACHE: dict[str, str] = {}


def _agent_text(obj: Any) -> str | None:
    """Best-effort extraction of an assistant/agent message from one JSONL
    event emitted by `codex exec --json`. The exact event schema varies across
    codex CLI versions, so this pokes at the shapes we know about rather than
    asserting one."""
    if not isinstance(obj, dict):
        return None
    obj_type = str(obj.get("type", ""))
    role = obj.get("role")
    if "agent_message" in obj_type or "assistant" in obj_type or role == "assistant":
        for key in ("text", "content", "message"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v
    item = obj.get("item")
    if isinstance(item, dict):
        found = _agent_text(item)
        if found:
            return found
    msg = obj.get("msg")
    if isinstance(msg, dict):
        found = _agent_text(msg)
        if found:
            return found
    return None


class CodexCLIProvider(Provider):
    """Provider backed by shelling out to the `codex` CLI in `exec` mode."""

    name = "codex_cli"
    supports_tools = False

    def __init__(self, pc: ProviderConfig):
        self.pc = pc
        self.binary = shutil.which("codex")
        if not self.binary:
            raise ProviderError(
                "The `codex` CLI was not found on PATH. Install OpenAI Codex CLI "
                "and authenticate with `codex login`, then retry."
            )
        self.timeout = pc.extra.get("timeout", _DEFAULT_TIMEOUT)

    # -- feature detection -------------------------------------------------
    def _help_text(self) -> str:
        if self.binary not in _HELP_CACHE:
            try:
                r = subprocess.run(
                    [self.binary, "exec", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                _HELP_CACHE[self.binary] = (r.stdout or "") + (r.stderr or "")
            except (subprocess.TimeoutExpired, OSError):
                _HELP_CACHE[self.binary] = ""
        return _HELP_CACHE[self.binary]

    # -- prompt assembly -----------------------------------------------------
    @staticmethod
    def _build_prompt(req: CompletionRequest) -> str:
        parts: list[str] = []
        if req.system:
            parts.append(f"[SYSTEM]\n{req.system}")
        for m in req.messages:
            role = m.role.upper()
            chunks: list[str] = []
            if m.content:
                chunks.append(m.content)
            if m.tool_calls:
                chunks.extend(
                    f"tool_call {tc.name}({json.dumps(tc.arguments)})" for tc in m.tool_calls
                )
            if m.tool_results:
                chunks.extend(f"tool_result[{tr.name}]: {tr.content}" for tr in m.tool_results)
            if chunks:
                parts.append(f"[{role}]\n" + "\n".join(chunks))
        return "\n\n".join(parts)

    # -- output parsing -------------------------------------------------
    @staticmethod
    def _parse_jsonl(stdout: str) -> str | None:
        texts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            text = _agent_text(obj)
            if text:
                texts.append(text)
        return texts[-1] if texts else None

    # -- public API -----------------------------------------------------
    def complete(self, req: CompletionRequest) -> CompletionResponse:
        prompt = self._build_prompt(req)
        help_text = self._help_text()
        use_json = "--json" in help_text

        cmd = [self.binary, "exec"]
        if use_json:
            cmd.append("--json")
        cmd.append(prompt)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired as e:
            raise ProviderError(
                f"`codex exec` timed out after {self.timeout}s. Increase "
                "`extra.timeout` on the codex provider config if the task is large."
            ) from e
        except OSError as e:
            raise ProviderError(f"Failed to run `codex` CLI: {e}") from e

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:800]
            raise ProviderError(f"`codex exec` failed (exit {result.returncode}): {detail}")

        text: str | None = None
        if use_json:
            text = self._parse_jsonl(result.stdout)
        if text is None:
            text = result.stdout.strip()

        return CompletionResponse(
            text=text,
            tool_calls=[],
            stop_reason="end",
            usage=Usage(),
            raw={"stdout": result.stdout, "stderr": result.stderr},
        )
