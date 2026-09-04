"""MCP server exposing web search tools over stdio."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .search import SearchError, fetch_page, search

server = MCPServer(name="web-search-mcp", version=__version__)


@server.tool(
    name="web_search",
    title="Web search",
    description=(
        "Search the web and return ranked results with titles, URLs, and snippets. "
        "Use this to discover sources for a topic or to find current information. "
        "Follow up with fetch_page to read any result in full."
    ),
)
def web_search(query: str, limit: int = 10) -> dict[str, Any]:
    """Search the web.

    Args:
        query: The search query. Be specific for better results.
        limit: Maximum number of results to return (1-50, default 10).
    """
    try:
        results, notes = search(query, limit=limit)
    except SearchError as exc:
        return {"query": query, "results": [], "count": 0, "error": str(exc)}
    except ValueError as exc:
        return {"query": query, "results": [], "count": 0, "error": str(exc)}

    payload: dict[str, Any] = {
        "query": query,
        "count": len(results),
        "engine": results[0].engine if results else None,
        "results": [r.as_dict() for r in results],
    }
    if notes:
        payload["notes"] = notes
    return payload


@server.tool(
    name="fetch_page",
    title="Fetch page content",
    description=(
        "Fetch a URL and extract its readable text, stripping navigation, scripts, and styles. "
        "Use this after web_search to read a specific result, or when you already have a URL."
    ),
)
def fetch_page_tool(url: str, max_chars: int = 8000) -> dict[str, Any]:
    """Fetch and extract text from a web page.

    Args:
        url: Absolute http(s) URL to fetch.
        max_chars: Maximum characters of text to return (200-50000, default 8000).
    """
    try:
        return fetch_page(url, max_chars=max_chars)
    except ValueError as exc:
        return {"url": url, "error": str(exc)}
    except Exception as exc:  # network/HTTP failures should not kill the session
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    server.run(transport="stdio")
