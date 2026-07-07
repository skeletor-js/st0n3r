"""Write-pipeline tests. No network: providers are scripted fakes."""

from __future__ import annotations

import sys
import types as pytypes
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.pipelines.common import chapter_context, render_prompt
from stoner.pipelines.write import run_archive, run_write
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.types import CompletionRequest, CompletionResponse, Usage

_CLEAN_PARAGRAPHS = [
    "He walked to the window and stood there a while. The yard was bare. A dog crossed the road and stopped, looked back, and went on.",
    "He thought of his father's hands, how they had held the plow, and of the dry fields in August. Nothing in the house moved.",
    "He had lived here forty years and knew each sound the boards made, and none came now. The kettle sat cold on the stove.",
    "In the morning there would be work: the fence along the north line, the gate that sagged, a letter he owed his brother.",
    "She had left the garden in October, and the beds still held their shapes under the frost, patient as graves.",
    "Once, when he was a boy, his mother had read to him at this table by lamplight, her voice low against the wind outside.",
    "The clock in the hall kept its slow count. He did not wind it anymore, yet somehow it went on, losing a minute a day.",
    "A truck passed on the county road, its lights sweeping the ceiling, and the room returned to dark behind it.",
    "He ate standing up, bread and the end of the ham, and washed the plate and set it in the rack where hers used to go.",
    "Later he took down the ledger and entered the day's figures in his careful hand: feed, diesel, the vet's bill from spring.",
    "Sleep came the way it always came now, without announcement, somewhere between one worry and the next.",
    "Before dawn the birds began, first one and then the whole hedge, and he lay listening as the window went gray.",
]
CLEAN_PROSE = "\n\n".join(_CLEAN_PARAGRAPHS)

ARCHIVIST_JSON = """{
  "summary": "William reflects at the window; the farm is failing.",
  "facts": [
    {"entity": "William", "kind": "character", "field": "home", "value": "the farm", "quote": "He had lived here forty years"}
  ],
  "new_entities": [],
  "thread_updates": []
}"""


class ScriptedProvider(Provider):
    """Returns queued responses in order; repeats the last one when empty."""

    name = "scripted"
    supports_tools = True

    def __init__(self, responses: list[CompletionResponse]):
        self.responses = list(responses)
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.requests.append(req)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def text_response(text: str) -> CompletionResponse:
    return CompletionResponse(text=text, stop_reason="end", usage=Usage(input_tokens=10, output_tokens=20))


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    p = WritingProject.create(tmp_path / "book", "testbook")
    scaffold_project(p, "testbook")
    return p


def test_render_prompt_fills_known_placeholders_only(project):
    ctx = chapter_context(project, 1)
    out = render_prompt("writer.md", ctx)
    assert "testbook" in out
    assert "{project_name}" not in out
    # The template's own doc-comment header is stripped (injected canon
    # content may still contain comments of its own).
    assert not out.startswith("<!--")


def test_run_write_fallback_save_and_archive(project):
    provider = ScriptedProvider([text_response(CLEAN_PROSE), text_response(ARCHIVIST_JSON)])
    res = run_write(project, 1, provider=provider)
    assert res.words > 200
    fm, body = project.read_chapter(1)
    assert "He walked to the window" in body
    assert res.slop_before >= 0
    assert res.gate_passed
    assert res.revision_loops == 0
    assert res.archive is not None
    assert len(res.archive.applied_facts) == 1
    # memory got a chapter summary
    mem = project.read_memory()
    assert "1" in mem.get("chapters", {})


def test_run_write_gate_triggers_revision(project, monkeypatch):
    sloppy = (
        "She couldn't help but delve into the tapestry of emotions — a testament "
        "to the myriad feelings that washed over her. Little did she know, her "
        "eyes widened as a palpable sense of dread hung in the air. "
    ) * 30
    calls = {"revise": 0}

    def fake_revise(project_, number, findings, model=None, provider=None):
        calls["revise"] += 1
        fm, _ = project_.read_chapter(number)
        project_.write_chapter(number, fm, CLEAN_PROSE)

        @dataclass
        class R:
            chapter: int = number
            old_words: int = 0
            new_words: int = 0
            applied: int = len(findings)
            summary: str = ""
            usage: Usage = field(default_factory=Usage)

        return R()

    stub = pytypes.ModuleType("stoner.review.revise")
    stub.revise_chapter = fake_revise
    monkeypatch.setitem(sys.modules, "stoner.review.revise", stub)

    provider = ScriptedProvider([text_response(sloppy), text_response(ARCHIVIST_JSON)])
    res = run_write(project, 2, provider=provider)
    assert calls["revise"] == 1
    assert res.revision_loops == 1
    assert res.slop_after < res.slop_before
    assert res.gate_passed


def test_run_write_agent_produced_nothing_raises(project):
    provider = ScriptedProvider([text_response("Sorry, I cannot.")])
    with pytest.raises(RuntimeError, match="without producing chapter"):
        run_write(project, 3, provider=provider)


def test_run_archive_preview_does_not_write(project):
    project.write_chapter(1, {"title": "One"}, CLEAN_PROSE)
    provider = ScriptedProvider([text_response(ARCHIVIST_JSON)])
    res = run_archive(project, 1, provider=provider, auto=False)
    assert res.dry_run
    assert len(res.applied_facts) == 1
    # nothing persisted in preview mode
    assert "1" not in project.read_memory().get("chapters", {})
