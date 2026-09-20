"""Backend selection, block detection, pagination, and the curl fallback."""

from __future__ import annotations

import httpx
import pytest

import web_tools.config as config
import web_tools.search as search_mod
from web_tools.config import SafeSearch, Settings
from web_tools.http_client import CurlUnavailable, RateLimiter, curl_request
from web_tools.search import is_block
from web_tools.server import fetch_page_tool, web_search

DDG_OK = """
<div class="result"><a class="result__a" href="https://b.example">B</a>
<a class="result__snippet">snip</a></div>
"""


@pytest.fixture
def settings(monkeypatch):
    """Swap the live settings for the duration of one test."""

    def install(**kwargs) -> Settings:
        replacement = Settings(**kwargs)
        monkeypatch.setattr(config, "settings", replacement)
        return replacement

    return install


class TestBlockDetection:
    @pytest.mark.parametrize("status", [202, 403])
    def test_status_signals(self, status):
        assert is_block(status, "<html>plausible looking page</html>") is True

    def test_empty_200_body(self):
        assert is_block(200, "   ") is True

    @pytest.mark.parametrize(
        "body",
        [
            "<div class='anomaly-modal'>x</div>",
            "please solve the CAPTCHA",
            "<title>Just a moment...</title>",
            "Enable JavaScript and cookies to continue",
            "cf-mitigated: challenge",
        ],
    )
    def test_challenge_bodies_at_200(self, body):
        assert is_block(200, body) is True

    def test_real_page_is_not_a_block(self):
        assert is_block(200, '<div class="result"><a class="result__a">ok</a></div>') is False


class TestCurlFallback:
    def test_auto_retries_blocked_engine_through_curl(self, mock_http, allow_curl):
        mock_http(lambda request: httpx.Response(202, text="anomaly"))
        allow_curl(lambda method, url, **kw: (200, DDG_OK, {}))

        results, notes = search_mod.search("q", backend="auto")
        assert results[0].engine == "duckduckgo"
        assert any("blocked on httpx" in n and "retried with curl" in n for n in notes)

    def test_auto_retries_after_a_transport_error(self, mock_http, allow_curl):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("tls reset", request=request)

        mock_http(handler)
        allow_curl(lambda method, url, **kw: (200, DDG_OK, {}))
        results, notes = search_mod.search("q", backend="auto")
        assert results[0].engine == "duckduckgo"
        assert any("httpx failed" in n for n in notes)

    def test_httpx_backend_never_calls_curl(self, mock_http, allow_curl):
        def boom(*a, **k):
            raise AssertionError("explicit httpx backend must not fall back")

        mock_http(lambda request: httpx.Response(202, text="anomaly"))
        allow_curl(boom)
        with pytest.raises(search_mod.SearchError):
            search_mod.search("q", backend="httpx")

    def test_curl_backend_is_used_directly(self, allow_curl):
        calls: list[str] = []

        def handler(method, url, **kw):
            calls.append(url)
            return (200, DDG_OK, {})

        allow_curl(handler)
        results, _ = search_mod.search("q", backend="curl")
        assert calls == ["https://html.duckduckgo.com/html/"]
        assert results[0].engine == "duckduckgo"

    def test_missing_curl_is_reported_not_raised(self, monkeypatch, settings):
        settings(search_backend="curl")

        def missing(*a, **k):
            raise CurlUnavailable("curl_cffi is not installed")

        monkeypatch.setattr(search_mod, "curl_request", missing)
        out = web_search("q")
        assert out["count"] == 0
        assert "curl_cffi is not installed" in out["error"]

    def test_no_results_suggests_the_browser_extra(self, mock_http, settings):
        settings(search_backend="httpx")
        mock_http(lambda request: httpx.Response(202, text="anomaly"))
        out = web_search("q")
        assert "curl_cffi" in out["error"] and "browser extra" in out["error"]


class TestRegionAndSafeSearch:
    def _captured(self, mock_http, **kwargs):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text=DDG_OK)

        mock_http(handler)
        search_mod.search("q", backend="httpx", **kwargs)
        return seen

    def test_region_reaches_the_engine(self, mock_http):
        seen = self._captured(mock_http, region="de-de")
        assert b"kl=de-de" in seen[0].content

    def test_safe_search_level_is_sent(self, mock_http):
        seen = self._captured(mock_http, safe_search=SafeSearch.STRICT)
        assert b"kp=1" in seen[0].content

    def test_server_default_applies_when_unset(self, mock_http, settings):
        settings(region="fr-fr", search_backend="httpx")
        seen = self._captured(mock_http)
        assert b"kl=fr-fr" in seen[0].content

    def test_tool_rejects_unknown_safe_search(self):
        out = web_search("q", safe_search="somewhat")
        assert "invalid safe_search" in out["error"]


class TestEngines:
    """Bing was removed for returning first-word-only results at HTTP 200."""

    def test_duckduckgo_is_the_only_engine(self):
        assert [e.name for e in search_mod._ENGINES] == ["duckduckgo"]

    def test_no_engine_targets_bing(self):
        assert not any("bing" in e.url for e in search_mod._ENGINES)


class TestAdFiltering:
    def test_sponsored_results_are_dropped(self):
        html = """
        <div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>
        <div class="result"><a class="result__a" href="https://real.example">Real</a></div>
        """
        results = search_mod.parse_duckduckgo(html, limit=10)
        assert [r.url for r in results] == ["https://real.example"]
        assert results[0].rank == 1


class TestPagination:
    def _page(self, mock_http, chars: int = 5000):
        html = "<html><body><main>" + ("ab " * chars) + "</main></body></html>"
        mock_http(lambda request: httpx.Response(200, text=html))

    def test_reports_total_and_next_offset(self, mock_http):
        self._page(mock_http)
        out = search_mod.fetch_page("https://example.com", max_chars=1000)
        assert out["start_index"] == 0
        assert out["length"] == 1000
        assert out["total_length"] > 1000
        assert out["next_start_index"] == 1000
        assert out["truncated"] is True

    def test_second_window_continues_where_the_first_stopped(self, mock_http):
        self._page(mock_http)
        first = search_mod.fetch_page("https://example.com", max_chars=1000)
        second = search_mod.fetch_page(
            "https://example.com", max_chars=1000, start_index=first["next_start_index"]
        )
        assert second["start_index"] == 1000
        assert second["content"] != first["content"]
        assert first["total_length"] == second["total_length"]

    def test_final_window_is_not_truncated(self, mock_http):
        mock_http(lambda request: httpx.Response(200, text="<main>short page</main>"))
        out = search_mod.fetch_page("https://example.com", max_chars=1000, start_index=6)
        assert out["truncated"] is False
        assert "next_start_index" not in out
        assert out["content"] == "page"

    def test_negative_start_index_is_clamped(self, mock_http):
        mock_http(lambda request: httpx.Response(200, text="<main>abc</main>"))
        assert search_mod.fetch_page("https://x.example", start_index=-5)["start_index"] == 0


class TestFetchBackendSelection:
    def test_cloudflare_challenge_at_200_retries_with_curl(self, mock_http, allow_curl):
        mock_http(lambda request: httpx.Response(200, text="<title>Just a moment...</title>"))
        allow_curl(lambda method, url, **kw: (200, "<main>real content</main>", {}))
        out = search_mod.fetch_page("https://cf.example", backend="auto")
        assert out["content"] == "real content"
        assert out["backend"] == "curl"

    def test_403_retries_with_curl(self, mock_http, allow_curl):
        mock_http(lambda request: httpx.Response(403, text="forbidden"))
        allow_curl(lambda method, url, **kw: (200, "<main>real content</main>", {}))
        assert search_mod.fetch_page("https://cf.example", backend="auto")["backend"] == "curl"

    def test_404_is_not_retried(self, mock_http, allow_curl):
        def boom(*a, **k):
            raise AssertionError("a genuine 404 must not be retried through curl")

        mock_http(lambda request: httpx.Response(404, text="nope"))
        allow_curl(boom)
        with pytest.raises(httpx.HTTPStatusError):
            search_mod.fetch_page("https://x.example/missing", backend="auto")

    def test_curl_redirects_are_followed_by_hand(self, allow_curl):
        hops = {
            "https://a.example/1": (302, "", {"location": "/2"}),
            "https://a.example/2": (200, "<main>done</main>", {}),
        }
        allow_curl(lambda method, url, **kw: hops[url])
        out = search_mod.fetch_page("https://a.example/1", backend="curl")
        assert out["content"] == "done"
        assert out["url"] == "https://a.example/2"

    def test_curl_error_status_surfaces(self, allow_curl):
        allow_curl(lambda method, url, **kw: (500, "boom", {}))
        with pytest.raises(httpx.HTTPStatusError):
            search_mod.fetch_page("https://a.example", backend="curl")

    def test_tool_rejects_unknown_backend(self):
        out = fetch_page_tool("https://example.com", backend="selenium")
        assert "invalid backend" in out["error"]

    def test_tool_reports_missing_curl(self, monkeypatch):
        monkeypatch.setattr(
            search_mod,
            "curl_request",
            lambda *a, **k: (_ for _ in ()).throw(CurlUnavailable("not installed")),
        )
        out = fetch_page_tool("https://example.com", backend="curl")
        assert out["error"] == "not installed"


class TestRateLimiter:
    def test_allows_requests_under_the_limit(self):
        limiter = RateLimiter(3)
        for _ in range(3):
            limiter.acquire()  # would block on the 4th

    def test_limit_tracks_a_callable(self):
        limit = 5
        limiter = RateLimiter(lambda: limit)
        assert limiter.requests_per_minute == 5
        limit = 9
        assert limiter.requests_per_minute == 9


class TestCurlUnavailable:
    def test_curl_request_raises_a_typed_error_when_uninstalled(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("curl_cffi"):
                raise ImportError("no curl_cffi")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(CurlUnavailable, match="browser"):
            curl_request("GET", "https://example.com")
