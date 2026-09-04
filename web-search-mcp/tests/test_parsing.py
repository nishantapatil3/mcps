import base64

import pytest

from web_search_mcp.search import (
    parse_bing,
    parse_duckduckgo,
    unwrap_bing_url,
    unwrap_duckduckgo_url,
)


def _bing_wrapper(target: str) -> str:
    payload = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc&u=a1{payload}&ntb=1"


class TestUnwrapBing:
    def test_decodes_wrapped_url(self):
        target = "https://modelcontextprotocol.io/docs/getting-started/intro"
        assert unwrap_bing_url(_bing_wrapper(target)) == target

    def test_passes_through_direct_url(self):
        assert unwrap_bing_url("https://example.com/a") == "https://example.com/a"

    def test_returns_original_on_undecodable_payload(self):
        bad = "https://www.bing.com/ck/a?u=a1!!!not-base64!!!"
        assert unwrap_bing_url(bad) == bad

    def test_returns_original_when_decoded_is_not_a_url(self):
        payload = base64.urlsafe_b64encode(b"not-a-url").decode().rstrip("=")
        url = f"https://www.bing.com/ck/a?u=a1{payload}"
        assert unwrap_bing_url(url) == url

    def test_missing_u_param(self):
        url = "https://www.bing.com/ck/a?p=1"
        assert unwrap_bing_url(url) == url


class TestUnwrapDuckDuckGo:
    def test_decodes_uddg(self):
        url = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fa%3D1"
        assert unwrap_duckduckgo_url(url) == "https://example.com/page?a=1"

    def test_adds_scheme_to_protocol_relative(self):
        assert unwrap_duckduckgo_url("//example.com/x") == "https://example.com/x"

    def test_passes_through_absolute(self):
        assert unwrap_duckduckgo_url("https://example.com") == "https://example.com"


BING_HTML = """
<html><body><ol id="b_results">
  <li class="b_algo">
    <h2><a href="{wrapped}">First Result</a></h2>
    <div class="b_caption"><p>Snippet   with

    whitespace</p></div>
  </li>
  <li class="b_algo">
    <h2><a href="https://direct.example.com">Second</a></h2>
    <p>Second snippet</p>
  </li>
  <li class="b_algo"><div>no anchor here</div></li>
</ol></body></html>
""".format(wrapped=_bing_wrapper("https://real.example.com/target"))


class TestParseBing:
    def test_extracts_and_normalizes(self):
        results = parse_bing(BING_HTML, limit=10)
        assert len(results) == 2
        first = results[0]
        assert first.title == "First Result"
        assert first.url == "https://real.example.com/target"
        assert first.snippet == "Snippet with whitespace"
        assert first.rank == 1
        assert first.engine == "bing"

    def test_ranks_are_sequential(self):
        results = parse_bing(BING_HTML, limit=10)
        assert [r.rank for r in results] == [1, 2]

    def test_respects_limit(self):
        assert len(parse_bing(BING_HTML, limit=1)) == 1

    def test_skips_entries_without_anchor(self):
        assert all(r.url for r in parse_bing(BING_HTML, limit=10))

    def test_empty_html(self):
        assert parse_bing("<html><body></body></html>", limit=10) == []


DDG_HTML = """
<html><body>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fdoc">Doc Title</a>
    <a class="result__snippet">A snippet</a>
  </div>
  <div class="result"><span>malformed, no anchor</span></div>
</body></html>
"""


class TestParseDuckDuckGo:
    def test_extracts_result(self):
        results = parse_duckduckgo(DDG_HTML, limit=10)
        assert len(results) == 1
        assert results[0].url == "https://example.org/doc"
        assert results[0].title == "Doc Title"
        assert results[0].snippet == "A snippet"
        assert results[0].engine == "duckduckgo"

    def test_empty_html(self):
        assert parse_duckduckgo("<html></html>", limit=5) == []
