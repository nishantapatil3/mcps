"""Live end-to-end checks. Marked `network` and skipped unless selected.

Run with:  uv run --group dev pytest -m network
"""

from __future__ import annotations

import pytest

import web_tools.search as search_mod

pytestmark = pytest.mark.network


def test_live_search_returns_usable_results():
    results, notes = search_mod.search("model context protocol specification", limit=5)
    assert results, f"no results; engine notes: {notes}"
    assert len(results) <= 5
    for r in results:
        assert r.title
        assert r.url.startswith(("http://", "https://"))
        assert r.engine == "duckduckgo"
        # redirect wrappers must have been unwrapped
        assert "uddg=" not in r.url
    assert [r.rank for r in results] == list(range(1, len(results) + 1))


def test_live_search_honors_every_query_term():
    """Guards the regression that got Bing removed.

    Bing answered a multi-word query with a generic SERP keyed off the first word
    only ('rust async runtime tokio benchmarks' returned the Rust video game), at
    HTTP 200 with well-formed markup. Only a relevance check catches that.
    """
    results, notes = search_mod.search("tokio async runtime benchmarks", limit=10)
    assert results, f"no results; engine notes: {notes}"
    haystack = " ".join(f"{r.title} {r.snippet} {r.url}" for r in results).lower()
    for term in ("tokio", "async"):
        assert term in haystack, f"'{term}' missing from all results; got {haystack[:300]!r}"


def test_live_fetch_page_extracts_text():
    out = search_mod.fetch_page("https://example.com", max_chars=2000)
    assert "error" not in out
    assert "Example Domain" in out["title"] or "Example Domain" in out["content"]


def test_live_fetch_page_paginates():
    first = search_mod.fetch_page("https://www.rfc-editor.org/rfc/rfc2616.txt", max_chars=1000)
    assert first["truncated"] is True
    second = search_mod.fetch_page(
        "https://www.rfc-editor.org/rfc/rfc2616.txt",
        max_chars=1000,
        start_index=first["next_start_index"],
    )
    assert second["start_index"] == 1000
    assert second["content"] and second["content"] != first["content"]


def test_live_ssrf_guard_blocks_the_metadata_endpoint():
    # Uses the real resolver: the guard must hold outside the mocked tests too.
    with pytest.raises(search_mod.BlockedURLError):
        search_mod.fetch_page("http://169.254.169.254/latest/meta-data/")
