"""Search backends that work over plain HTTP, with no browser and no API key.

DuckDuckGo's HTML endpoint is the only engine. Bing was removed: it answers a
plain client with a syntactically valid SERP whose results ignore every term
after the first, so `python list comprehension performance` returned the
Python.org homepage. That failure is undetectable from the response - HTTP 200,
well-formed `li.b_algo` markup, ten parseable results - so it silently shadowed
the working engine instead of falling through to it.

Engines are tried in order until one yields results. Parsing is a pure function
from (html, limit) -> list[SearchResult], which keeps it testable against saved
fixtures without network access. Transport is separate (see `http_client`) so an
engine can be retried through curl_cffi's Chrome TLS fingerprint when the plain
client gets fingerprint-blocked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from . import config
from .config import SafeSearch
from .http_client import (
    BASE_HEADERS,
    USER_AGENT,
    CurlUnavailable,
    RateLimiter,
    build_client,
    curl_available,
    curl_request,
)
from .ssrf import BlockedURLError, validate_public_url

__all__ = [
    "BASE_HEADERS",
    "USER_AGENT",
    "BlockedURLError",
    "SearchError",
    "SearchResult",
    "fetch_page",
    "looks_like_challenge",
    "parse_duckduckgo",
    "search",
    "unwrap_duckduckgo_url",
]

# Redirect handling for fetch_page. Hops are followed by hand rather than by the
# client so the SSRF guard runs on every one - otherwise a public URL could
# bounce the fetch into the private network in a single redirect.
MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# Cloudflare / bot-filter interstitials that arrive with a 200 status. Seeing one
# on the httpx path under "auto" is the cue to retry with curl.
_CHALLENGE_BODY_SIGNALS = (
    "anomaly-modal",
    "detected unusual",
    "captcha",
    "cf-mitigated",
    "just a moment...",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
)

_search_limiter = RateLimiter(lambda: config.settings.requests_per_minute)
_fetch_limiter = RateLimiter(lambda: config.settings.requests_per_minute)


class SearchError(RuntimeError):
    """Raised when every engine fails to return usable results."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    rank: int
    engine: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def unwrap_duckduckgo_url(url: str) -> str:
    """DuckDuckGo HTML results sometimes proxy through /l/?uddg=<encoded>."""
    if "uddg=" not in url:
        return url if not url.startswith("//") else f"https:{url}"
    target = parse_qs(urlparse(url if "://" in url else f"https:{url}").query).get("uddg", [""])[0]
    return unquote(target) if target else url


def parse_duckduckgo(html: str, limit: int) -> list[SearchResult]:
    tree = HTMLParser(html)
    results: list[SearchResult] = []
    for node in tree.css("div.result, div.web-result"):
        anchor = node.css_first("a.result__a")
        if anchor is None:
            continue
        href = anchor.attributes.get("href")
        if not href:
            continue
        # Sponsored results are routed through /y.js and are not organic hits.
        if "y.js" in href:
            continue
        snippet = node.css_first("a.result__snippet") or node.css_first("div.result__snippet")
        results.append(
            SearchResult(
                title=_clean(anchor.text()),
                url=unwrap_duckduckgo_url(href),
                snippet=_clean(snippet.text() if snippet else ""),
                rank=len(results) + 1,
                engine="duckduckgo",
            )
        )
        if len(results) >= limit:
            break
    return results


def is_block(status: int, body: str) -> bool:
    """Detect a soft block from the status and body together.

    Status alone is not enough: DuckDuckGo answers a TLS fingerprint it dislikes
    with an empty HTTP 202, and Cloudflare serves its interstitial at 200. Both
    are 2xx, so `raise_for_status` never fires and the page parses to zero
    results without any error to explain why.
    """
    if status in (202, 403):
        return True
    if status == 200 and not (body or "").strip():
        return True
    sample = (body or "")[:20000].lower()
    return any(token in sample for token in _CHALLENGE_BODY_SIGNALS)


def looks_like_challenge(response: Any) -> bool:
    """`is_block` for anything exposing `.status_code` and `.text`."""
    return is_block(response.status_code, response.text)


@dataclass(frozen=True)
class _Engine:
    name: str
    method: str
    url: str
    parser: Callable[[str, int], list[SearchResult]]
    headers: dict[str, str] = field(default_factory=dict)

    def payload(self, query: str, region: str, safe_search: SafeSearch) -> dict[str, Any]:
        """Build the query params (GET) or form fields (POST) for this engine."""
        return {
            "q": query,
            "b": "",
            "kl": region,  # region / language
            "kp": safe_search.value,  # SafeSearch level
        }


_ENGINES = (
    _Engine(
        "duckduckgo",
        "POST",
        "https://html.duckduckgo.com/html/",
        parse_duckduckgo,
        headers={"Referer": "https://duckduckgo.com/"},
    ),
)


def _build_client(timeout: float = 20.0) -> httpx.Client:
    """Seam for tests to inject a mock transport. See `http_client.build_client`."""
    return build_client(timeout, verify=config.settings.ssl_verify)


def _request_httpx(
    client: httpx.Client, engine: _Engine, payload: dict[str, Any]
) -> tuple[int, str]:
    if engine.method == "GET":
        response = client.get(engine.url, params=payload, headers=engine.headers)
    else:
        response = client.post(engine.url, data=payload, headers=engine.headers)
    return response.status_code, response.text


def _request_curl(engine: _Engine, payload: dict[str, Any], timeout: float) -> tuple[int, str]:
    status, body, _ = curl_request(
        engine.method,
        engine.url,
        params=payload if engine.method == "GET" else None,
        data=payload if engine.method != "GET" else None,
        timeout=timeout,
        verify=config.settings.ssl_verify,
    )
    return status, body


def _engine_html(
    engine: _Engine,
    payload: dict[str, Any],
    backend: str,
    client: httpx.Client,
    timeout: float,
    notes: list[str],
) -> tuple[int, str] | None:
    """Fetch one engine's HTML, applying the backend policy.

    Returns None when the engine could not be reached at all; the reason is
    appended to `notes` so a partial failure is reported rather than swallowed.
    """
    if backend == "curl":
        try:
            return _request_curl(engine, payload, timeout)
        except CurlUnavailable as exc:
            notes.append(f"{engine.name}: {exc}")
            return None
        except Exception as exc:  # curl_cffi raises its own exception hierarchy
            notes.append(f"{engine.name}: curl request failed ({type(exc).__name__})")
            return None

    try:
        status, body = _request_httpx(client, engine, payload)
    except httpx.HTTPError as exc:
        if backend != "auto":
            notes.append(f"{engine.name}: request failed ({type(exc).__name__})")
            return None
        # A rejected TLS handshake surfaces as a transport error, not a status,
        # so curl's separate network stack is worth one attempt. On a genuine
        # outage it fails fast rather than masking the original error.
        notes.append(f"{engine.name}: httpx failed ({type(exc).__name__}); retried with curl")
        return _engine_html(engine, payload, "curl", client, timeout, notes)

    if backend == "auto" and is_block(status, body) and curl_available():
        notes.append(f"{engine.name}: blocked on httpx (HTTP {status}); retried with curl")
        return _engine_html(engine, payload, "curl", client, timeout, notes)
    return status, body


def search(
    query: str,
    limit: int = 10,
    timeout: float = 20.0,
    region: str | None = None,
    safe_search: SafeSearch | None = None,
    backend: str | None = None,
) -> tuple[list[SearchResult], list[str]]:
    """Run `query` against each engine in order, returning the first hit set.

    Returns the results plus human-readable notes describing any engine that was
    skipped or retried, so callers can surface partial degradation.
    """
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), 50))
    region = config.settings.region if region is None else region.strip()
    safe_search = safe_search or config.settings.safe_search
    backend = backend or config.settings.search_backend

    _search_limiter.acquire()
    notes: list[str] = []
    with _build_client(timeout) as client:
        for engine in _ENGINES:
            outcome = _engine_html(
                engine, engine.payload(query, region, safe_search), backend, client, timeout, notes
            )
            if outcome is None:
                continue
            status, body = outcome

            if is_block(status, body):
                notes.append(f"{engine.name}: rate limited or challenged (HTTP {status})")
                continue
            if status >= 400:
                notes.append(f"{engine.name}: HTTP {status}")
                continue

            results = engine.parser(body, limit)
            if results:
                return results, notes
            notes.append(f"{engine.name}: no results parsed")

    if not curl_available():
        notes.append(
            "no results on any engine; adding the browser extra "
            "(uvx --with 'curl_cffi>=0.7' ...) enables Chrome TLS impersonation, "
            "which usually clears fingerprint-based blocks"
        )
    raise SearchError("; ".join(notes) or "all engines failed")


def _guard(url: str) -> None:
    """Apply the SSRF guard unless private URLs were explicitly allowed."""
    if not config.settings.allow_private_urls:
        validate_public_url(url)


def _fetch_html_httpx(url: str, timeout: float) -> tuple[str, str]:
    """GET `url` via httpx, validating the target and every redirect hop."""
    with _build_client(timeout) as client:
        # Redirects are followed by hand below so the guard sees each hop.
        client.follow_redirects = False
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            _guard(current)
            response = client.get(current)
            location = response.headers.get("location")
            if response.status_code in REDIRECT_STATUSES and location:
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            return str(response.url), response.text
    raise httpx.HTTPError(f"too many redirects (>{MAX_REDIRECTS})")


def _fetch_html_curl(url: str, timeout: float) -> tuple[str, str]:
    """GET `url` via curl_cffi, validating the target and every redirect hop."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _guard(current)
        status, body, headers = curl_request(
            "GET",
            current,
            timeout=timeout,
            verify=config.settings.ssl_verify,
            allow_redirects=False,
        )
        location = headers.get("location") or headers.get("Location")
        if status in REDIRECT_STATUSES and location:
            current = urljoin(current, location)
            continue
        if status >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {status} for {current}",
                request=httpx.Request("GET", current),
                response=httpx.Response(status, text=body),
            )
        return current, body
    raise httpx.HTTPError(f"too many redirects (>{MAX_REDIRECTS})")


def _fetch_html(url: str, backend: str, timeout: float) -> tuple[str, str, str]:
    """Fetch page HTML per the backend policy. Returns (final_url, html, backend_used)."""
    if backend == "curl":
        final_url, html = _fetch_html_curl(url, timeout)
        return final_url, html, "curl"

    try:
        final_url, html = _fetch_html_httpx(url, timeout)
    except httpx.HTTPStatusError as exc:
        # 403 on a plain client is the classic fingerprint rejection; anything
        # else is a real error and should reach the caller unchanged.
        if backend == "auto" and exc.response.status_code == 403 and curl_available():
            final_url, html = _fetch_html_curl(url, timeout)
            return final_url, html, "curl"
        raise

    if backend == "auto" and is_block(200, html) and curl_available():
        final_url, html = _fetch_html_curl(url, timeout)
        return final_url, html, "curl"
    return final_url, html, "httpx"


def fetch_page(
    url: str,
    max_chars: int = 8000,
    timeout: float = 20.0,
    start_index: int = 0,
    backend: str | None = None,
) -> dict[str, Any]:
    """Fetch a URL and return a window of its readable text.

    `start_index` paginates: a long page is read across several calls rather than
    forcing one oversized response, and `next_start_index` in the result points at
    the following window when there is more to read.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    max_chars = max(200, min(int(max_chars), 50000))
    start_index = max(0, int(start_index))
    backend = backend or config.settings.fetch_backend

    _fetch_limiter.acquire()
    final_url, html, backend_used = _fetch_html(url, backend, timeout)

    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript", "nav", "header", "footer", "aside", "form"):
        for node in tree.css(tag):
            node.decompose()

    body = tree.css_first("main") or tree.css_first("article") or tree.body
    text = _clean(body.text(separator=" ")) if body else ""
    title_node = tree.css_first("title")

    window = text[start_index : start_index + max_chars]
    truncated = start_index + max_chars < len(text)

    payload: dict[str, Any] = {
        "url": final_url,
        "title": _clean(title_node.text() if title_node else ""),
        "content": window,
        "truncated": truncated,
        "length": len(window),
        "total_length": len(text),
        "start_index": start_index,
        "backend": backend_used,
    }
    if truncated:
        payload["next_start_index"] = start_index + max_chars
    return payload
