"""Search backends that work over plain HTTP, with no browser and no API key.

Engines are tried in order until one yields results. Each engine is a pure
function from (html, limit) -> list[SearchResult], which keeps parsing
testable against saved fixtures without network access.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from selectolax.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


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


def unwrap_bing_url(url: str) -> str:
    """Bing wraps results in /ck/a redirects with the target in a base64 `u` param.

    The param is prefixed with "a1" and uses url-safe base64 without padding.
    Returns the original URL unchanged when it is not a wrapper or cannot be
    decoded, so a format change degrades to a usable link rather than an error.
    """
    if "bing.com/ck/a" not in url:
        return url
    target = parse_qs(urlparse(url).query).get("u", [""])[0]
    if not target.startswith("a1"):
        return url
    payload = target[2:]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        candidate = decoded.decode("utf-8", errors="strict")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return url
    return candidate if candidate.startswith(("http://", "https://")) else url


def unwrap_duckduckgo_url(url: str) -> str:
    """DuckDuckGo HTML results sometimes proxy through /l/?uddg=<encoded>."""
    if "uddg=" not in url:
        return url if not url.startswith("//") else f"https:{url}"
    target = parse_qs(urlparse(url if "://" in url else f"https:{url}").query).get("uddg", [""])[0]
    return unquote(target) if target else url


def parse_bing(html: str, limit: int) -> list[SearchResult]:
    tree = HTMLParser(html)
    results: list[SearchResult] = []
    for node in tree.css("li.b_algo"):
        anchor = node.css_first("h2 a")
        if anchor is None:
            continue
        href = anchor.attributes.get("href")
        if not href:
            continue
        caption = node.css_first("div.b_caption p") or node.css_first("p")
        results.append(
            SearchResult(
                title=_clean(anchor.text()),
                url=unwrap_bing_url(href),
                snippet=_clean(caption.text() if caption else ""),
                rank=len(results) + 1,
                engine="bing",
            )
        )
        if len(results) >= limit:
            break
    return results


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


def looks_like_challenge(response: httpx.Response) -> bool:
    """Detect soft blocks that arrive as HTTP 200/202 with a challenge body.

    DuckDuckGo returns 202 with an "anomaly" interstitial rather than a 4xx,
    so status code alone is not a reliable signal.
    """
    if response.status_code == 202:
        return True
    body = response.text[:20000].lower()
    return any(token in body for token in ("anomaly-modal", "detected unusual", "captcha"))


# Each engine: (name, method, url, payload key)
_ENGINES = (
    ("bing", "GET", "https://www.bing.com/search", parse_bing),
    ("duckduckgo", "POST", "https://html.duckduckgo.com/html/", parse_duckduckgo),
)


def _build_client(timeout: float) -> httpx.Client:
    """Construct the HTTP client.

    Exists as a single seam so tests can inject a mock transport without
    monkeypatching the httpx module globally.
    """
    return httpx.Client(timeout=timeout, follow_redirects=True, headers=BASE_HEADERS)


def search(query: str, limit: int = 10, timeout: float = 20.0) -> tuple[list[SearchResult], list[str]]:
    """Run `query` against each engine in order, returning the first hit set.

    Returns the results plus a list of human-readable notes describing any
    engines that were skipped, so callers can surface partial degradation.
    """
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), 50))

    notes: list[str] = []
    with _build_client(timeout) as client:
        for name, method, url, parser in _ENGINES:
            try:
                if method == "GET":
                    response = client.get(url, params={"q": query})
                else:
                    response = client.post(
                        url, data={"q": query}, headers={"Referer": "https://duckduckgo.com/"}
                    )
            except httpx.HTTPError as exc:
                notes.append(f"{name}: request failed ({type(exc).__name__})")
                continue

            if looks_like_challenge(response):
                notes.append(f"{name}: rate limited or challenged (HTTP {response.status_code})")
                continue
            if response.status_code >= 400:
                notes.append(f"{name}: HTTP {response.status_code}")
                continue

            results = parser(response.text, limit)
            if results:
                return results, notes
            notes.append(f"{name}: no results parsed")

    raise SearchError("; ".join(notes) or "all engines failed")


def fetch_page(url: str, max_chars: int = 8000, timeout: float = 20.0) -> dict[str, Any]:
    """Fetch a URL and return its readable text as plain markdown-ish content."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    max_chars = max(200, min(int(max_chars), 50000))

    with _build_client(timeout) as client:
        response = client.get(url)
    response.raise_for_status()

    tree = HTMLParser(response.text)
    for tag in ("script", "style", "noscript", "nav", "header", "footer", "aside", "form"):
        for node in tree.css(tag):
            node.decompose()

    body = tree.css_first("main") or tree.css_first("article") or tree.body
    text = _clean(body.text(separator=" ")) if body else ""
    title_node = tree.css_first("title")

    return {
        "url": str(response.url),
        "title": _clean(title_node.text() if title_node else ""),
        "content": text[:max_chars],
        "truncated": len(text) > max_chars,
        "length": min(len(text), max_chars),
    }
