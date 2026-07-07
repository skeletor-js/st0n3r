"""The tool-calling agent loop.

`Agent.run()` drives one provider back and forth with the tool registry until
the model stops asking for tools, the turn/token budget runs out, or the
loop-guard trips on a stuck agent repeating an identical call. The full
transcript is written to `.stoner/sessions/<ts>-<session_name>.json`
incrementally (after every turn) so a crashed run still leaves useful state
on disk.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ledger import Ledger
from ..project import WritingProject
from ..providers.base import Provider
from ..types import CompletionRequest, Message, ToolCall, ToolResult, Usage
from .textproto import (
    FORMAT_CORRECTION,
    looks_like_attempted_call,
    parse_response,
    render_tools_prompt,
)
from .tools import ToolRegistry

_REPEAT_NUDGE_AT = 3
_REPEAT_STOP_AT = 4


@dataclass
class AgentResult:
    text: str
    turns: int
    usage: Usage
    transcript_path: str


_MAX_FORMAT_RETRIES = 3


def _call_signature(calls: list[ToolCall]) -> tuple:
    return tuple(sorted((c.name, json.dumps(c.arguments, sort_keys=True)) for c in calls))


@dataclass
class _Transcript:
    """Incrementally-written JSON transcript for one agent run."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


class Agent:
    """Runs the tool-calling loop for one model against one project."""

    def __init__(
        self,
        provider: Provider,
        model_id: str,
        tools: ToolRegistry,
        project: WritingProject,
        ledger: Ledger,
        session_name: str = "",
    ):
        self.provider = provider
        self.model_id = model_id
        self.tools = tools
        self.project = project
        self.ledger = ledger
        self.session_name = session_name or "agent"

    # ------------------------------------------------------------------
    def run(
        self,
        task: str,
        system: str,
        max_turns: int = 24,
        max_tokens_budget: int = 200_000,
    ) -> AgentResult:
        ts = time.strftime("%Y%m%dT%H%M%S")
        transcript_path = self.project.resolve(
            f".stoner/sessions/{ts}-{self.session_name}.json"
        )
        transcript = _Transcript(
            path=transcript_path,
            data={
                "session": self.session_name,
                "model": self.model_id,
                "task": task,
                "system": system,
                "started_at": time.time(),
                "turns": [],
            },
        )

        specs = self.tools.specs()
        native_tools = self.provider.supports_tools
        effective_system = system
        if specs and not native_tools:
            effective_system = f"{system}\n\n{render_tools_prompt(specs)}".strip()

        messages: list[Message] = [Message(role="user", content=task)]
        total_usage = Usage()
        last_signature: tuple | None = None
        repeat_count = 0
        final_text = ""
        turns = 0

        format_retries = 0
        while turns < max_turns:
            turns += 1
            req = CompletionRequest(
                model=self.model_id,
                system=effective_system,
                messages=messages,
                tools=specs if native_tools else [],
                max_tokens=self.project.config.max_tokens,
                temperature=self.project.config.temperature,
            )
            resp = self.provider.complete(req)
            total_usage = total_usage + resp.usage

            if native_tools:
                calls = resp.tool_calls
                assistant_text = resp.text
            else:
                assistant_text, calls = parse_response(resp.text)

            assistant_msg = Message(role="assistant", content=assistant_text, tool_calls=calls)
            messages.append(assistant_msg)
            final_text = assistant_text

            self.ledger.append(
                "agent.turn",
                target=self.session_name,
                session=self.session_name,
                turn=turns,
                tool_calls=[c.name for c in calls],
                stop_reason=resp.stop_reason,
            )

            turn_record: dict[str, Any] = {
                "turn": turns,
                "stop_reason": resp.stop_reason,
                "assistant_text": assistant_text,
                "tool_calls": [c.model_dump() for c in calls],
                "usage": resp.usage.model_dump(),
            }

            if not calls and not native_tools and format_retries < _MAX_FORMAT_RETRIES:
                if looks_like_attempted_call(assistant_text, self.tools.names()):
                    format_retries += 1
                    messages.append(Message(role="user", content=FORMAT_CORRECTION))
                    turn_record["format_retry"] = format_retries
                    transcript.data["turns"].append(turn_record)
                    transcript.save()
                    continue

            if not calls:
                transcript.data["turns"].append(turn_record)
                transcript.data["final_usage"] = total_usage.model_dump()
                transcript.save()
                break

            signature = _call_signature(calls)
            if signature == last_signature:
                repeat_count += 1
            else:
                repeat_count = 1
                last_signature = signature

            if repeat_count >= _REPEAT_STOP_AT:
                final_text = (
                    f"{assistant_text}\n\n"
                    "[agent stopped: same tool call repeated "
                    f"{repeat_count} times in a row]"
                ).strip()
                turn_record["assistant_text"] = final_text
                turn_record["loop_guard"] = "stopped"
                transcript.data["turns"].append(turn_record)
                transcript.data["final_usage"] = total_usage.model_dump()
                transcript.save()
                break

            results: list[ToolResult] = [self.tools.execute(self.project, c) for c in calls]
            turn_record["tool_results"] = [r.model_dump() for r in results]

            nudge = ""
            if repeat_count == _REPEAT_NUDGE_AT:
                nudge = (
                    "[note: you have called the same tool with identical arguments "
                    f"{repeat_count} times in a row. Try something different or stop.]"
                )
                turn_record["loop_guard"] = "nudged"

            messages.append(Message(role="tool", tool_results=results))
            if nudge:
                # A separate user message: provider mappers drop `content` on
                # tool-role messages that carry tool_results, so a nudge
                # placed there would never reach the model.
                messages.append(Message(role="user", content=nudge))
            transcript.data["turns"].append(turn_record)
            transcript.save()

            if total_usage.input_tokens + total_usage.output_tokens >= max_tokens_budget:
                final_text = f"{final_text}\n\n[agent stopped: token budget exhausted]".strip()
                transcript.data["final_usage"] = total_usage.model_dump()
                transcript.data["stopped_reason"] = "budget_exhausted"
                transcript.save()
                break
        else:
            transcript.data["stopped_reason"] = "max_turns"
            transcript.data["final_usage"] = total_usage.model_dump()
            transcript.save()

        return AgentResult(
            text=final_text,
            turns=turns,
            usage=total_usage,
            transcript_path=str(transcript_path),
        )
