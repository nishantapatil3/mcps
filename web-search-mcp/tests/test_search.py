"""Tests for engine fallback, challenge detection, and tool-level behavior."""

from __future__ import annotations

import httpx
import pytest

import web_search_mcp.search as search_mod
from web_search_mcp.search import SearchError, looks_like_challenge
from web_search_mcp.server import fetch_page_tool, web_search


def _response(status: int, text: str) -> httpx.Response:
    return httpx.Response(status_code=status, text=text, request=httpx.Request("GET", "https://x"))


class TestChallengeDetection:
    def test_202_is_a_challenge(self):
        assert looks_like_challenge(_response(202, "<html>anything</html>")) is True

    def test_anomaly_body_on_200_is_a_challenge(self):
        assert looks_like_challenge(_response(200, "<div class='anomaly-modal'>x</div>")) is True

    def test_captcha_body_is_a_challenge(self):
        assert looks_like_challenge(_response(200, "please solve the CAPTCHA")) is True

    def test_normal_page_is_not_a_challenge(self):
        assert looks_like_challenge(_response(200, "<div class='result'>ok</div>")) is False


DDG_OK = """
<div class="result"><a class="result__a" href="https://b.example">B</a>
<a class="result__snippet">snip</a></div>
"""


class TestFallback:
    def test_uses_duckduckgo_when_healthy(self, mock_http):
        mock_http(lambda request: httpx.Response(200, text=DDG_OK))
        results, notes = search_mod.search("q", limit=5)
        assert [r.engine for r in results] == ["duckduckgo"]
        assert notes == []

    def test_raises_when_the_engine_is_challenged(self, mock_http):
        mock_http(lambda request: httpx.Response(202, text="anomaly"))
        with pytest.raises(SearchError) as excinfo:
            search_mod.search("q")
        assert "duckduckgo" in str(excinfo.value)

    def test_transport_error_is_reported(self, mock_http):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        mock_http(handler)
        with pytest.raises(SearchError) as excinfo:
            search_mod.search("q")
        assert "ConnectError" in str(excinfo.value)

    def test_http_error_status_is_noted(self, mock_http):
        mock_http(lambda request: httpx.Response(503, text="unavailable"))
        with pytest.raises(SearchError) as excinfo:
            search_mod.search("q")
        assert "503" in str(excinfo.value)

    def test_unparseable_body_is_noted(self, mock_http):
        mock_http(lambda request: httpx.Response(200, text="<html><body>nothing</body></html>"))
        with pytest.raises(SearchError) as excinfo:
            search_mod.search("q")
        assert "no results parsed" in str(excinfo.value)


class TestValidation:
    def test_empty_query_rejected(self):
        with pytest.raises(ValueError):
            search_mod.search("   ")

    def test_limit_is_clamped_without_error(self, mock_http):
        mock_http(lambda request: httpx.Response(200, text=DDG_OK))
        results, _ = search_mod.search("q", limit=9999)
        assert len(results) <= 50

    def test_fetch_page_rejects_non_http_scheme(self):
        with pytest.raises(ValueError):
            search_mod.fetch_page("file:///etc/passwd")


class TestToolContract:
    """Tools must return structured errors, never raise, so a session survives."""

    def test_web_search_returns_error_dict_on_failure(self, mock_http):
        mock_http(lambda request: httpx.Response(202, text="anomaly"))
        out = web_search("anything")
        assert out["count"] == 0 and out["results"] == [] and "error" in out

    def test_web_search_empty_query_returns_error_dict(self):
        out = web_search("  ")
        assert "error" in out

    def test_web_search_success_shape(self, mock_http):
        mock_http(lambda request: httpx.Response(200, text=DDG_OK))
        out = web_search("q", limit=3)
        assert out["count"] == 1
        assert out["engine"] == "duckduckgo"
        assert set(out["results"][0]) == {"title", "url", "snippet", "rank", "engine"}

    def test_fetch_page_tool_returns_error_dict(self):
        out = fetch_page_tool("not-a-url")
        assert "error" in out

    def test_fetch_page_tool_wraps_network_failure(self, mock_http):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        mock_http(handler)
        out = fetch_page_tool("https://example.com")
        assert "error" in out and "ConnectTimeout" in out["error"]


class TestFetchPage:
    def test_strips_boilerplate_and_extracts_main(self, mock_http):
        html = """
        <html><head><title>  My   Page </title><style>.a{}</style></head>
        <body><nav>skip me</nav><script>bad()</script>
        <main><p>Real content here.</p></main>
        <footer>footer junk</footer></body></html>
        """
        mock_http(lambda request: httpx.Response(200, text=html))
        out = search_mod.fetch_page("https://example.com")
        assert out["title"] == "My Page"
        assert "Real content here." in out["content"]
        assert "skip me" not in out["content"]
        assert "bad()" not in out["content"]
        assert "footer junk" not in out["content"]
        assert out["truncated"] is False

    def test_truncation_flag(self, mock_http):
        html = "<html><body><main>" + ("word " * 5000) + "</main></body></html>"
        mock_http(lambda request: httpx.Response(200, text=html))
        out = search_mod.fetch_page("https://example.com", max_chars=200)
        assert out["truncated"] is True
        assert out["length"] == 200

    def test_raises_for_http_error_status(self, mock_http):
        mock_http(lambda request: httpx.Response(404, text="nope"))
        with pytest.raises(httpx.HTTPStatusError):
            search_mod.fetch_page("https://example.com/missing")
