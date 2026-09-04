from web_search_mcp.search import parse_duckduckgo, unwrap_duckduckgo_url


class TestUnwrapDuckDuckGo:
    def test_decodes_uddg(self):
        url = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fa%3D1"
        assert unwrap_duckduckgo_url(url) == "https://example.com/page?a=1"

    def test_adds_scheme_to_protocol_relative(self):
        assert unwrap_duckduckgo_url("//example.com/x") == "https://example.com/x"

    def test_passes_through_absolute(self):
        assert unwrap_duckduckgo_url("https://example.com") == "https://example.com"


DDG_HTML = """
<html><body>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fdoc">Doc Title</a>
    <a class="result__snippet">A   snippet   with
    whitespace</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://direct.example.com">Second</a>
    <div class="result__snippet">Second snippet</div>
  </div>
  <div class="result"><span>malformed, no anchor</span></div>
</body></html>
"""


class TestParseDuckDuckGo:
    def test_extracts_result(self):
        results = parse_duckduckgo(DDG_HTML, limit=10)
        assert len(results) == 2
        assert results[0].url == "https://example.org/doc"
        assert results[0].title == "Doc Title"
        assert results[0].snippet == "A snippet with whitespace"
        assert results[0].rank == 1
        assert results[0].engine == "duckduckgo"

    def test_ranks_are_sequential(self):
        assert [r.rank for r in parse_duckduckgo(DDG_HTML, limit=10)] == [1, 2]

    def test_respects_limit(self):
        assert len(parse_duckduckgo(DDG_HTML, limit=1)) == 1

    def test_skips_entries_without_anchor(self):
        assert all(r.url for r in parse_duckduckgo(DDG_HTML, limit=10))

    def test_empty_html(self):
        assert parse_duckduckgo("<html></html>", limit=5) == []
