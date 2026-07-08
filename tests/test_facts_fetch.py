"""Tests for the harness-side web_fetch tool (U3). Zero real network.

All fetches go through httpx.MockTransport, so no socket is ever opened.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from stoner.config import FactsConfig, StonerConfig
from stoner.facts.webfetch import fetch_readable, make_web_fetch_tool
from stoner.ledger import Ledger
from stoner.project import WritingProject

_HTML = (
    "<html><head><title>Humboldt County Code</title>"
    "<style>.x{color:red}</style><script>evil()</script></head>"
    "<body><nav>menu menu</nav>"
    "<p>Category II violations carry a $1,200 reinspection fee.</p>"
    "<p>Cure period is 60 days.</p></body></html>"
)


def _transport(html=_HTML, status=200, content_type="text/html"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": content_type}, text=html)

    return httpx.MockTransport(handler)


@pytest.fixture
def project(tmp_path: Path) -> WritingProject:
    return WritingProject.create(tmp_path / "book", "Book")


def _config(enabled=True, allowed=None):
    return StonerConfig(
        project_name="Book",
        facts=FactsConfig(enabled=enabled, allowed_domains=allowed or []),
    )


# ---------------------------------------------------------------------------
# fetch_readable
# ---------------------------------------------------------------------------


def test_fetch_readable_extracts_text_and_title():
    res = fetch_readable("https://humboldtgov.org/code", transport=_transport())
    assert res.error == ""
    assert res.title == "Humboldt County Code"
    assert "$1,200 reinspection fee" in res.text
    assert "evil()" not in res.text  # script stripped
    assert "color:red" not in res.text  # style stripped
    assert "menu menu" not in res.text  # nav stripped


def test_fetch_readable_truncates_oversized_body():
    big = "<html><body>" + ("A" * 5000) + "</body></html>"
    res = fetch_readable(
        "https://x.org", max_bytes=1000, transport=_transport(html=big)
    )
    assert res.truncated is True
    assert len(res.text) <= 1000


def test_fetch_readable_non_text_content_type_errors():
    res = fetch_readable(
        "https://x.org/data.json", transport=_transport(content_type="application/json")
    )
    assert res.error.startswith("ERROR:")
    assert "not text" in res.error


def test_fetch_readable_connection_error_no_raise():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    res = fetch_readable("https://x.org", transport=httpx.MockTransport(boom))
    assert res.error.startswith("ERROR:")
    assert res.text == ""


def test_fetch_readable_http_error_status():
    res = fetch_readable("https://x.org/missing", transport=_transport(status=404))
    assert "HTTP 404" in res.error


def test_fetch_readable_rejects_non_http_scheme():
    res = fetch_readable("file:///etc/passwd")
    assert "http(s)" in res.error


# ---------------------------------------------------------------------------
# make_web_fetch_tool
# ---------------------------------------------------------------------------


def _ledger_lines(project: WritingProject) -> list[dict]:
    path = project.root / ".stoner" / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_tool_refuses_when_disabled(project: WritingProject, monkeypatch):
    tool = make_web_fetch_tool(project, Ledger(project.root), _config(enabled=False))
    out = tool(project, url="https://x.org")
    assert out.startswith("ERROR:")
    assert "facts.enabled" in out
    # Disabled feature never touches the network layer, so no ledger line.
    assert _ledger_lines(project) == []


def test_tool_off_list_domain_is_ledgered(project: WritingProject):
    tool = make_web_fetch_tool(
        project,
        Ledger(project.root),
        _config(allowed=["humboldtgov.org"]),
        transport=_transport(),
    )
    out = tool(project, url="https://evil.example.com/x")
    assert out.startswith("ERROR:")
    assert "allowed_domains" in out
    lines = _ledger_lines(project)
    assert len(lines) == 1
    assert lines[0]["action"] == "facts.fetch"
    assert "evil.example.com" in lines[0]["target"]


def test_tool_success_ledgers_one_line_with_url(project: WritingProject):
    tool = make_web_fetch_tool(
        project, Ledger(project.root), _config(), transport=_transport()
    )
    out = tool(project, url="https://humboldtgov.org/code")
    assert "$1,200 reinspection fee" in out
    lines = _ledger_lines(project)
    assert len(lines) == 1
    assert lines[0]["action"] == "facts.fetch"
    assert lines[0]["target"] == "https://humboldtgov.org/code"
    assert lines[0]["detail"]["status"] == 200


def test_tool_empty_url_errors(project: WritingProject):
    tool = make_web_fetch_tool(project, Ledger(project.root), _config())
    assert tool(project, url="").startswith("ERROR:")
