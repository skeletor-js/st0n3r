"""Fenced-JSON tool-calling protocol for providers with `supports_tools=False`.

When a provider can't do native function-calling (e.g. `codex_cli`), the
engine appends a rendered tool catalog to the system prompt and asks the
model to reply with prose and/or a single fenced ` ```tool_call ` block per
call. This module renders that catalog and parses it back out.
"""

from __future__ import annotations

import json
import re

from ..types import ToolCall, ToolSpec

_FENCE_RE = re.compile(
    r"```tool_call\s*\n(?P<body>.*?)```", re.DOTALL | re.IGNORECASE
)

_PROTOCOL_HEADER = """## Tool use

You have access to the following tools. To call one, reply with a fenced
code block, and nothing else meaningful outside it (surrounding prose is
fine but the call itself must be a standalone block):

```tool_call
{"name": "<tool name>", "arguments": {"<arg>": <value>, ...}}
```

Rules:
- Use exactly one fenced ```tool_call block per tool invocation.
- You may include more than one such block in a single reply if you need to
  call more than one tool before seeing results.
- The JSON body must be a single object with a "name" string and an
  "arguments" object (use `{}` for no-argument tools).
- If you are not calling a tool, just reply normally with no ```tool_call
  block.
- Never wrap the JSON in extra prose inside the fence itself.

Available tools:
"""


def render_tools_prompt(tools: list[ToolSpec]) -> str:
    """Render a system-prompt section describing available tools and the
    fenced-JSON calling convention. Returns "" if there are no tools."""
    if not tools:
        return ""
    lines = [_PROTOCOL_HEADER]
    for t in tools:
        schema = json.dumps(t.parameters or {"type": "object", "properties": {}})
        lines.append(f"- `{t.name}`: {t.description}\n  parameters schema: {schema}")
    return "\n".join(lines)


def parse_response(text: str) -> tuple[str, list[ToolCall]]:
    """Split a model reply into (clean_text, tool_calls).

    Tolerant of: leading/trailing prose, multiple fences, and malformed JSON
    (skipped, with a note appended to clean_text so the failure is visible).
    """
    calls: list[ToolCall] = []
    notes: list[str] = []

    def _consume(match: re.Match) -> str:
        body = match.group("body").strip()
        try:
            obj = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            notes.append(f"[tool_call parse error: invalid JSON: {body[:200]!r}]")
            return ""
        if not isinstance(obj, dict) or "name" not in obj:
            notes.append(f"[tool_call parse error: missing 'name' field: {body[:200]!r}]")
            return ""
        name = obj.get("name")
        if not isinstance(name, str):
            notes.append(f"[tool_call parse error: 'name' is not a string: {body[:200]!r}]")
            return ""
        arguments = obj.get("arguments", {})
        if not isinstance(arguments, dict):
            notes.append(
                f"[tool_call parse error: 'arguments' is not an object for {name!r}]"
            )
            arguments = {}
        calls.append(ToolCall(name=name, arguments=arguments))
        return ""

    clean_text = _FENCE_RE.sub(_consume, text).strip()
    if notes:
        clean_text = (clean_text + "\n\n" + "\n".join(notes)).strip()
    return clean_text, calls


_CALLISH_RE = re.compile(r"(```\s*tool|\btool_call\b|\"name\"\s*:)", re.IGNORECASE)


def looks_like_attempted_call(text: str, tool_names: list[str]) -> bool:
    """Heuristic: the reply contains no valid fenced call but appears to be
    trying to call a tool (freehand `query_canon({...})`, a stray
    ```tool fence, or bare protocol JSON). Used by the agent loop to send a
    format correction instead of treating the reply as a final answer."""
    if _CALLISH_RE.search(text):
        return True
    return any(re.search(rf"\b{re.escape(n)}\s*\(", text) for n in tool_names)


FORMAT_CORRECTION = (
    "Your last reply looked like a tool call but was not in the required "
    "format, so nothing was executed. To call a tool, reply with a fenced "
    "block exactly like this:\n\n"
    '```tool_call\n{"name": "query_canon", "arguments": {"topic": "example"}}\n```\n\n'
    "Try again now. If you did not mean to call a tool, continue with your task."
)
