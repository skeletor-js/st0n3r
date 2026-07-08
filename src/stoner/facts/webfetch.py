"""Harness-side `web_fetch`: a ledgered, capped, opt-in URL fetcher.

This is the degradation path when no provider-native web search exists: a
tool-capable model can be handed `web_fetch` to pull URLs it is given (via
`--url` seeds) or already knows, and quote from them. It does a single capped
GET (timeout, size, redirect caps, text/* only) and reduces HTML to readable
text with a stdlib `html.parser` tag stripper -- no `readability`/`lxml`/
`trafilatura` heavy deps (invariant 8). The extraction is deliberately crude:
the consumer is a model quoting passages, not a rendering engine.

Every attempt -- success, HTTP error, refusal -- appends one `facts.fetch`
ledger line carrying the URL, so the network trail is complete. The tool is
opt-in (`facts.enabled`) and honors the domain allowlist; it is NOT part of
`engine/tools.default_registry()` -- only the research pipeline binds it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from ..config import StonerConfig
from ..ledger import Ledger
from ..project import WritingProject

_USER_AGENT = "st0n3r-facts/0.1 (+harness web_fetch; single user-directed fetch)"
_DEFAULT_TIMEOUT = 20.0
_MAX_BYTES = 400_000  # cap on fetched body size
_MAX_REDIRECTS = 5

# Tags whose text content is never readable prose.
_SKIP_TAGS = {"script", "style", "head", "nav", "noscript", "template", "svg", "footer"}
# Tags that imply a line break in the reduced text.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "ul", "ol", "table", "blockquote",
}


@dataclass
class FetchResult:
    """Outcome of one fetch. `error` is set (an ERROR: string) on any failure."""

    url: str
    title: str = ""
    text: str = ""
    truncated: bool = False
    status: int = 0
    error: str = ""


class _Reducer(HTMLParser):
    """Collect readable text and the document title from HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def _reduce_html(html: str) -> tuple[str, str]:
    """Return (title, readable_text) from an HTML string."""
    reducer = _Reducer()
    try:
        reducer.feed(html)
        reducer.close()
    except Exception:  # noqa: BLE001 - a malformed page must never raise
        pass
    return reducer.title.strip(), reducer.text()


def _domain_allowed(url: str, allowed_domains: list[str]) -> bool:
    host = urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    for d in allowed_domains:
        d = d.strip().lower()
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def fetch_readable(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _MAX_BYTES,
    allowed_domains: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    """Fetch one URL and reduce it to readable text.

    Never raises: transport, HTTP, content-type, and allowlist failures all
    come back as a `FetchResult` with `error` set to an `ERROR: ...` string.
    `transport` is for tests (httpx.MockTransport); production leaves it None.
    """
    url = (url or "").strip()
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        return FetchResult(url=url, error="ERROR: only http(s) URLs can be fetched")

    if allowed_domains and not _domain_allowed(url, allowed_domains):
        host = urlparse(url).netloc
        return FetchResult(
            url=url,
            error=f"ERROR: {host or url} is not in the configured facts.allowed_domains list",
        )

    client_kwargs: dict[str, object] = {
        "follow_redirects": True,
        "timeout": timeout,
        "max_redirects": _MAX_REDIRECTS,
        "headers": {"User-Agent": _USER_AGENT},
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    try:
        with httpx.Client(**client_kwargs) as client:  # type: ignore[arg-type]
            resp = client.get(url)
    except httpx.HTTPError as e:
        return FetchResult(url=url, error=f"ERROR: could not fetch {url}: {e}")

    final_url = str(resp.url)
    if resp.status_code >= 400:
        return FetchResult(
            url=final_url,
            status=resp.status_code,
            error=f"ERROR: {url} returned HTTP {resp.status_code}",
        )

    content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not content_type.startswith("text/"):
        return FetchResult(
            url=final_url,
            status=resp.status_code,
            error=f"ERROR: {url} is {content_type or 'an unknown type'}, not text; not fetched",
        )

    body = resp.content
    truncated = False
    if len(body) > max_bytes:
        body = body[:max_bytes]
        truncated = True
    html = body.decode(resp.encoding or "utf-8", errors="replace")
    title, text = _reduce_html(html)
    return FetchResult(
        url=final_url,
        title=title,
        text=text,
        truncated=truncated,
        status=resp.status_code,
    )


def make_web_fetch_tool(
    project: WritingProject,
    ledger: Ledger,
    config: StonerConfig,
    *,
    transport: httpx.BaseTransport | None = None,
):
    """Build the `web_fetch` tool function, bound to a project/ledger/config.

    Matches the `engine/tools` convention: `(project, **kwargs) -> str`, never
    raises, returns `ERROR: ...` strings on failure. Enforces `facts.enabled`
    and the domain allowlist, and ledgers `facts.fetch` on every attempt that
    reaches the network layer (allowlist refusals included). `transport` is a
    test seam (httpx.MockTransport); production leaves it None.
    """
    facts_cfg = config.facts

    def web_fetch(project: WritingProject, url: str = "", **_ignored: object) -> str:
        target = str(url or "").strip()
        if not facts_cfg.enabled:
            return (
                "ERROR: web fetch is disabled. Set `facts.enabled: true` in stoner.yaml "
                "to allow network access (opt-in)."
            )
        if not target:
            return "ERROR: url must not be empty"

        result = fetch_readable(
            target,
            allowed_domains=facts_cfg.allowed_domains or None,
            transport=transport,
        )
        ledger.append(
            "facts.fetch",
            target=result.url or target,
            status=result.status,
            bytes=len(result.text.encode("utf-8")),
            error=bool(result.error),
            truncated=result.truncated,
        )
        if result.error:
            return result.error

        head = f"# {result.title}\n({result.url})\n\n" if result.title else f"({result.url})\n\n"
        text = result.text
        if result.truncated:
            text += "\n\n[truncated at fetch size cap]"
        return head + text

    return web_fetch
