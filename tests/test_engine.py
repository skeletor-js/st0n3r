"""Tests for the agent loop, tool registry, and fenced-JSON tool protocol.

No network calls: a scripted FakeProvider stands in for a real vendor
backend so the agent loop can be exercised end-to-end against a real
(temp-dir) WritingProject.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from stoner.engine import textproto
from stoner.engine.agent import Agent
from stoner.engine.tools import ToolRegistry, default_registry
from stoner.ledger import Ledger
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.types import CompletionRequest, CompletionResponse, ToolCall, ToolSpec, Usage

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Test Book")
    proj.write("canon/premise.md", "# Premise\nA test book about tests.\n")
    proj.write("canon/style.md", "# Style\nThird person, past tense.\n")
    return proj


class FakeProvider(Provider):
    """Scripted provider: either a fixed list of responses (last one repeats
    once exhausted) or a callable `(turn_index) -> CompletionResponse`."""

    name = "fake"

    def __init__(
        self,
        responses: list[CompletionResponse] | Callable[[int], CompletionResponse],
        supports_tools: bool = True,
    ):
        self.responses = responses
        self.supports_tools = supports_tools
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        if callable(self.responses):
            return self.responses(idx)
        return self.responses[min(idx, len(self.responses) - 1)]


def make_agent(project: WritingProject, provider: Provider, session_name="test") -> Agent:
    return Agent(
        provider=provider,
        model_id="fake/model",
        tools=default_registry(),
        project=project,
        ledger=Ledger(project.root),
        session_name=session_name,
    )


# ---------------------------------------------------------------------------
# textproto
# ---------------------------------------------------------------------------


def test_render_tools_prompt_empty():
    assert textproto.render_tools_prompt([]) == ""


def test_render_tools_prompt_lists_tools():
    tools = [ToolSpec(name="ping", description="pings", parameters={"type": "object"})]
    rendered = textproto.render_tools_prompt(tools)
    assert "ping" in rendered
    assert "tool_call" in rendered


def test_parse_response_plain_text_no_fence():
    text, calls = textproto.parse_response("just a normal reply, no tools needed")
    assert calls == []
    assert text == "just a normal reply, no tools needed"


def test_parse_response_single_fence_with_surrounding_prose():
    raw = (
        "Sure, let me check that.\n\n"
        '```tool_call\n{"name": "read_chapter", "arguments": {"number": 1}}\n```\n'
        "\nI'll wait for the result."
    )
    text, calls = textproto.parse_response(raw)
    assert len(calls) == 1
    assert calls[0].name == "read_chapter"
    assert calls[0].arguments == {"number": 1}
    assert "tool_call" not in text
    assert "Sure, let me check" in text


def test_parse_response_multiple_fences():
    raw = (
        '```tool_call\n{"name": "a", "arguments": {}}\n```\n'
        "some text between\n"
        '```tool_call\n{"name": "b", "arguments": {"x": 1}}\n```'
    )
    text, calls = textproto.parse_response(raw)
    assert [c.name for c in calls] == ["a", "b"]
    assert calls[1].arguments == {"x": 1}
    assert "some text between" in text


def test_parse_response_malformed_json_is_skipped_with_note():
    raw = '```tool_call\n{not valid json\n```'
    text, calls = textproto.parse_response(raw)
    assert calls == []
    assert "parse error" in text.lower()


def test_parse_response_missing_name_field_skipped():
    raw = '```tool_call\n{"arguments": {}}\n```'
    text, calls = textproto.parse_response(raw)
    assert calls == []
    assert "parse error" in text.lower()


def test_parse_response_non_dict_arguments_defaults_to_empty():
    raw = '```tool_call\n{"name": "ping", "arguments": "not-a-dict"}\n```'
    text, calls = textproto.parse_response(raw)
    assert len(calls) == 1
    assert calls[0].arguments == {}
    assert "parse error" in text.lower()


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def test_write_then_read_chapter_roundtrip(project):
    reg = default_registry()
    out = reg.execute(
        project,
        ToolCall(
            name="write_chapter",
            arguments={"number": 1, "title": "The Start", "body": "It was a dark and testy night."},
        ),
    )
    assert not out.is_error
    assert "Wrote" in out.content

    out2 = reg.execute(project, ToolCall(name="read_chapter", arguments={"number": 1}))
    assert not out2.is_error
    assert "dark and testy night" in out2.content


def test_write_chapter_rejects_empty_body(project):
    reg = default_registry()
    out = reg.execute(
        project, ToolCall(name="write_chapter", arguments={"number": 1, "title": "x", "body": "  "})
    )
    assert out.is_error
    assert out.content.startswith("ERROR:")


def test_list_project_reports_chapters(project):
    reg = default_registry()
    reg.execute(
        project,
        ToolCall(name="write_chapter", arguments={"number": 1, "title": "T", "body": "some words here"}),
    )
    out = reg.execute(project, ToolCall(name="list_project", arguments={}))
    data = json.loads(out.content)
    assert data["summary"]["chapters"] == 1
    assert data["chapters"][0]["number"] == 1


def test_search_text_scopes(project):
    reg = default_registry()
    project.write("manuscript/ch-01.md", "A dragon flew over the castle.")
    project.write("canon/world/dragons.md", "Dragons are ancient and rare.")

    all_hits = reg.execute(project, ToolCall(name="search_text", arguments={"query": "dragon"}))
    assert "ch-01.md" in all_hits.content
    assert "dragons.md" in all_hits.content

    canon_only = reg.execute(
        project, ToolCall(name="search_text", arguments={"query": "dragon", "scope": "canon"})
    )
    assert "dragons.md" in canon_only.content
    assert "ch-01.md" not in canon_only.content

    no_hits = reg.execute(
        project, ToolCall(name="search_text", arguments={"query": "nonexistentxyz"})
    )
    assert "No matches" in no_hits.content

    bad_scope = reg.execute(
        project, ToolCall(name="search_text", arguments={"query": "dragon", "scope": "bogus"})
    )
    assert bad_scope.is_error


def test_query_canon_list_and_topic(project):
    reg = default_registry()
    listing = reg.execute(project, ToolCall(name="query_canon", arguments={}))
    assert "premise.md" in listing.content
    assert "style.md" in listing.content

    hit = reg.execute(project, ToolCall(name="query_canon", arguments={"topic": "premise"}))
    assert "A test book about tests" in hit.content

    miss = reg.execute(project, ToolCall(name="query_canon", arguments={"topic": "nonexistent"}))
    assert miss.is_error


def test_update_canon_rejects_paths_outside_canon(project):
    reg = default_registry()
    out = reg.execute(
        project, ToolCall(name="update_canon", arguments={"path": "notes/scratch.md", "content": "x"})
    )
    assert out.is_error

    ok = reg.execute(
        project,
        ToolCall(
            name="update_canon",
            arguments={"path": "canon/world/mars.md", "content": "Mars is red."},
        ),
    )
    assert not ok.is_error
    assert project.read("canon/world/mars.md") == "Mars is red."


def test_read_outline_missing_returns_error(project):
    reg = default_registry()
    out = reg.execute(project, ToolCall(name="read_outline", arguments={}))
    assert out.is_error


def test_update_beats_then_read_outline(project):
    reg = default_registry()
    reg.execute(
        project, ToolCall(name="update_beats", arguments={"chapter": 3, "content": "Beat one.\n"})
    )
    out = reg.execute(project, ToolCall(name="read_outline", arguments={"chapter": 3}))
    assert out.content == "Beat one.\n"


def test_get_memory_defaults_to_empty_object(project):
    reg = default_registry()
    out = reg.execute(project, ToolCall(name="get_memory", arguments={}))
    assert json.loads(out.content) == {}


def test_word_count_total_and_per_chapter(project):
    reg = default_registry()
    reg.execute(
        project,
        ToolCall(name="write_chapter", arguments={"number": 1, "title": "T", "body": "one two three"}),
    )
    total = reg.execute(project, ToolCall(name="word_count", arguments={}))
    data = json.loads(total.content)
    assert data["total"] == 3

    per_chapter = reg.execute(project, ToolCall(name="word_count", arguments={"chapter": 1}))
    assert "3 words" in per_chapter.content


def test_registry_unknown_tool_returns_error_result(project):
    reg = default_registry()
    out = reg.execute(project, ToolCall(name="does_not_exist", arguments={}))
    assert out.is_error
    assert "unknown tool" in out.content.lower()


def test_registry_never_raises_on_buggy_tool(project):
    reg = ToolRegistry()

    def _boom(project, **kwargs):
        raise RuntimeError("kaboom")

    reg.register(ToolSpec(name="boom", description="explodes", parameters={}), _boom)
    out = reg.execute(project, ToolCall(name="boom", arguments={}))
    assert out.is_error
    assert "kaboom" in out.content


# ---------------------------------------------------------------------------
# agent loop
# ---------------------------------------------------------------------------


def test_agent_executes_tool_calls_and_stops_on_no_calls(project):
    responses = [
        CompletionResponse(
            text="I'll draft it.",
            tool_calls=[
                ToolCall(
                    name="write_chapter",
                    arguments={"number": 1, "title": "Opening", "body": "Once there was a testable chapter."},
                )
            ],
            stop_reason="tool_use",
            usage=Usage(input_tokens=50, output_tokens=50),
        ),
        CompletionResponse(
            text="Done, wrote chapter 1.",
            tool_calls=[],
            stop_reason="end",
            usage=Usage(input_tokens=10, output_tokens=5),
        ),
    ]
    provider = FakeProvider(responses, supports_tools=True)
    agent = make_agent(project, provider, session_name="draft-ch1")

    result = agent.run(task="write chapter 1", system="you are a writer")

    assert result.turns == 2
    assert result.text == "Done, wrote chapter 1."
    assert result.usage.input_tokens == 60
    assert result.usage.output_tokens == 55

    # tool call actually executed against the real project
    assert project.resolve("manuscript/ch-01.md").exists()
    assert "testable chapter" in project.read("manuscript/ch-01.md")

    # transcript written incrementally, valid JSON, two turns recorded
    transcript = json.loads(open(result.transcript_path, encoding="utf-8").read())
    assert len(transcript["turns"]) == 2
    assert transcript["turns"][0]["tool_calls"][0]["name"] == "write_chapter"

    # ledger got an entry per turn
    entries = Ledger(project.root).tail(10)
    assert len(entries) == 2
    assert all(e.action == "agent.turn" for e in entries)


def test_agent_textproto_fallback_path(project):
    fenced = (
        "Let me look at the project first.\n\n"
        '```tool_call\n{"name": "list_project", "arguments": {}}\n```'
    )
    responses = [
        CompletionResponse(text=fenced, tool_calls=[], stop_reason="end", usage=Usage()),
        CompletionResponse(text="All done.", tool_calls=[], stop_reason="end", usage=Usage()),
    ]
    provider = FakeProvider(responses, supports_tools=False)
    agent = make_agent(project, provider, session_name="textproto")

    result = agent.run(task="check project status", system="you are a writer")

    assert result.turns == 2
    assert result.text == "All done."

    # first request's system prompt was augmented with the tool catalog
    first_req = provider.requests[0]
    assert "tool_call" in first_req.system
    assert first_req.tools == []  # native tools must not be sent for non-tool providers

    transcript = json.loads(open(result.transcript_path, encoding="utf-8").read())
    assert transcript["turns"][0]["tool_calls"][0]["name"] == "list_project"


def test_agent_loop_guard_stops_on_repeated_identical_call(project):
    def respond(idx: int) -> CompletionResponse:
        return CompletionResponse(
            text=f"checking again (turn {idx})",
            tool_calls=[ToolCall(name="list_project", arguments={})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    provider = FakeProvider(respond, supports_tools=True)
    agent = make_agent(project, provider, session_name="stuck")

    result = agent.run(task="loop forever", system="you are a writer", max_turns=24)

    assert result.turns == 4  # nudge at 3rd repeat, hard stop at 4th
    assert "repeated" in result.text.lower()


def test_agent_stops_on_token_budget_exhaustion(project):
    def respond(idx: int) -> CompletionResponse:
        return CompletionResponse(
            text=f"turn {idx}",
            tool_calls=[ToolCall(name="word_count", arguments={"chapter": idx + 1})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=100_000, output_tokens=100_000),
        )

    provider = FakeProvider(respond, supports_tools=True)
    agent = make_agent(project, provider, session_name="budget")

    result = agent.run(
        task="do a lot of work", system="you are a writer", max_turns=24, max_tokens_budget=1000
    )

    assert result.turns == 1
    assert "budget exhausted" in result.text.lower()


def test_agent_stops_on_max_turns(project):
    def respond(idx: int) -> CompletionResponse:
        return CompletionResponse(
            text=f"turn {idx}",
            tool_calls=[ToolCall(name="word_count", arguments={"chapter": idx + 1})],
            stop_reason="tool_use",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    provider = FakeProvider(respond, supports_tools=True)
    agent = make_agent(project, provider, session_name="maxturns")

    result = agent.run(task="keep going", system="you are a writer", max_turns=3)

    assert result.turns == 3
    transcript = json.loads(open(result.transcript_path, encoding="utf-8").read())
    assert transcript["stopped_reason"] == "max_turns"
