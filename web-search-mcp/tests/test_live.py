"""Live end-to-end checks. Marked `network` and skipped unless selected.

Run with:  uv run --group dev pytest -m network
"""

from __future__ import annotations

import pytest

import web_search_mcp.search as search_mod

pytestmark = pytest.mark.network


def test_live_search_returns_usable_results():
    results, notes = search_mod.search("model context protocol specification", limit=5)
    assert results, f"no results; engine notes: {notes}"
    assert len(results) <= 5
    for r in results:
        assert r.title
        assert r.url.startswith(("http://", "https://"))
        # redirect wrappers must have been unwrapped
        assert "bing.com/ck/a" not in r.url
        assert "uddg=" not in r.url
    assert [r.rank for r in results] == list(range(1, len(results) + 1))


def test_live_fetch_page_extracts_text():
    out = search_mod.fetch_page("https://example.com", max_chars=2000)
    assert "error" not in out
    assert "Example Domain" in out["title"] or "Example Domain" in out["content"]
