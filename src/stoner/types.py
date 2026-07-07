"""Shared data models for the st0n3r harness.

Everything that crosses a module boundary lives here so providers, engine,
slop, review, and UI agree on shapes without importing each other.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Chat / provider layer
# ---------------------------------------------------------------------------

Role = Literal["system", "user", "assistant", "tool"]


class ToolSpec(BaseModel):
    """A tool the agent exposes to the model (JSON-schema parameters)."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """A tool invocation requested by the model."""

    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    call_id: str
    name: str
    content: str
    is_error: bool = False


class Message(BaseModel):
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class CompletionRequest(BaseModel):
    model: str
    system: str = ""
    messages: list[Message] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    max_tokens: int = 8192
    temperature: float | None = None


class CompletionResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: Literal["end", "tool_use", "max_tokens", "error"] = "end"
    usage: Usage = Field(default_factory=Usage)
    raw: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Findings (shared by slop detector and review passes)
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    info = "info"
    minor = "minor"
    major = "major"
    critical = "critical"


class Span(BaseModel):
    """Character offsets into the source document, plus 1-based line."""

    start: int
    end: int
    line: int


class Finding(BaseModel):
    """One issue surfaced by an analyzer or critic pass."""

    id: str = Field(default_factory=lambda: f"f_{uuid.uuid4().hex[:10]}")
    source: str  # e.g. "slop:lexicon", "review:continuity"
    severity: Severity = Severity.minor
    category: str = ""  # analyzer/pass-specific bucket
    span: Span | None = None
    quote: str = ""  # offending text (short)
    issue: str = ""  # what is wrong
    suggestion: str = ""  # how to fix (may be empty)
    status: Literal["open", "accepted", "dismissed", "fixed"] = "open"


class SlopReport(BaseModel):
    """Result of running the slop detector over one document."""

    path: str
    score: float  # 0 clean .. 100 slop
    subscores: dict[str, float] = Field(default_factory=dict)  # per analyzer
    findings: list[Finding] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)  # word count, etc.
    created_at: float = Field(default_factory=time.time)


class ReviewReport(BaseModel):
    """Result of running critic passes over one document."""

    path: str
    passes: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""
    model: str = ""
    usage: Usage = Field(default_factory=Usage)
    created_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class LedgerEntry(BaseModel):
    ts: float = Field(default_factory=time.time)
    action: str  # e.g. "write.draft", "slop.check", "canon.update"
    target: str = ""  # usually a project-relative path
    detail: dict[str, Any] = Field(default_factory=dict)
    session: str = ""  # agent session id if applicable
