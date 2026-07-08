"""Tests for the verisimilitude sweep pass (U5)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.canon.store import CanonStore
from stoner.facts.locker import apply_facts
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.review.passes import PASSES, build_context
from stoner.review.runner import run_review
from stoner.types import CompletionRequest, CompletionResponse, Severity, Usage

CH_BODY = (
    "Ruth read the notice twice.\n\n"
    "The Category II reinspection fee was $500, the clerk said, due in thirty days.\n\n"
    "She had filed the CDFA-102 form the previous spring.\n"
)


@pytest.fixture
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Test Book")
    scaffold_project(proj, "Test Book")
    proj.write_chapter(1, {"title": "Notice", "pov": "Ruth"}, CH_BODY)
    return proj


def _seed_fee(project: WritingProject) -> None:
    apply_facts(
        CanonStore(project),
        [
            {
                "name": "Category II reinspection fee",
                "claim": "Humboldt Category II violations carry a $1,200 reinspection fee",
                "source_url": "https://humboldtgov.org/code",
                "confidence": "high",
                "quote": "$1,200 reinspection fee",
            }
        ],
        auto=True,
    )


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, responses: list | Callable[[int], CompletionResponse]):
        self.responses = responses
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        item = self.responses(idx) if callable(self.responses) else self.responses[
            min(idx, len(self.responses) - 1)
        ]
        return item


def _json(payload: dict) -> CompletionResponse:
    return CompletionResponse(text="```json\n" + json.dumps(payload) + "\n```", usage=Usage())


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_pass_registered_but_not_default():
    assert "verisimilitude" in PASSES
    from stoner.config import StonerConfig

    assert "verisimilitude" not in StonerConfig().review_passes


def test_import_cycle_safe_both_orders():
    # order 1 already exercised by the module import above; check order 2.
    import importlib

    import stoner.facts.sweep as sweep

    importlib.reload(sweep)
    import stoner.review.passes as passes

    assert "verisimilitude" in passes.PASSES


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def test_contradiction_finding_located(project: WritingProject):
    _seed_fee(project)
    payload = {
        "findings": [
            {
                "severity": "major",
                "category": "contradiction",
                "quote": "The Category II reinspection fee was $500",
                "issue": "Chapter says $500 but locker [category-ii-reinspection-fee] says $1,200.",
                "suggestion": "Change to $1,200.",
            }
        ],
        "summary": "one contradiction",
    }
    report = run_review(project, 1, passes=["verisimilitude"], provider=FakeProvider([_json(payload)]))
    finding = next(f for f in report.findings if f.category == "contradiction")
    assert finding.severity == Severity.major
    assert finding.span is not None  # quote located verbatim
    assert finding.span.line >= 1


def test_check_this_finding_minor(project: WritingProject):
    _seed_fee(project)
    payload = {
        "findings": [
            {
                "severity": "minor",
                "category": "check-this",
                "quote": "She had filed the CDFA-102 form",
                "issue": "Verify that CDFA-102 is the correct form name.",
                "suggestion": "",
            }
        ],
        "summary": "one check-this",
    }
    report = run_review(project, 1, passes=["verisimilitude"], provider=FakeProvider([_json(payload)]))
    finding = next(f for f in report.findings if f.category == "check-this")
    assert finding.severity == Severity.minor
    assert "verify" in finding.issue.lower()


def test_empty_locker_digest_empty_but_pass_runs(project: WritingProject):
    # No facts seeded -> facts_digest is empty.
    ctx = build_context(project, 1)
    assert ctx.facts_digest == ""
    _system, user = PASSES["verisimilitude"].build_prompt(ctx)
    assert "locker is empty" in user

    payload = {"findings": [], "summary": "nothing to flag"}
    report = run_review(project, 1, passes=["verisimilitude"], provider=FakeProvider([_json(payload)]))
    assert report.passes == ["verisimilitude"]


def test_runner_saves_report_and_ledgers(project: WritingProject):
    _seed_fee(project)
    payload = {"findings": [], "summary": "clean"}
    run_review(project, 1, passes=["verisimilitude"], provider=FakeProvider([_json(payload)]))
    reviews = list((project.root / ".stoner/reviews").glob("ch-01-*.json"))
    assert reviews, "review report saved"
    ledger = (project.root / ".stoner/ledger.jsonl").read_text()
    assert "review.run" in ledger


def test_garbage_response_degrades_to_info(project: WritingProject):
    _seed_fee(project)
    report = run_review(
        project, 1, passes=["verisimilitude"], provider=FakeProvider([CompletionResponse(text="not json")])
    )
    # Runner contract: an unparseable response becomes one info finding.
    assert any(f.severity == Severity.info for f in report.findings)
