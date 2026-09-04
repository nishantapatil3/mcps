# mcps

Model Context Protocol servers, runnable directly from this repo with `uvx` — no
cloning, no global installs, no Docker.

## web-search-mcp

Web search and page fetching for any MCP client. Uses plain HTTP requests
against public search endpoints, so there is **no browser, no chromedriver, and
no API key**.

### Tools

| Tool | Purpose |
| --- | --- |
| `web_search` | Search the web; returns ranked results with title, URL, snippet, and source engine. |
| `fetch_page` | Fetch a URL and extract readable text, stripping nav/script/style boilerplate. |

`web_search` accepts `query` and an optional `limit` (1–50, default 10).
`fetch_page` accepts `url` and an optional `max_chars` (200–50000, default 8000).

### Install

#### OpenCode

`~/.config/opencode/opencode.json`

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "web-search": {
      "type": "local",
      "command": [
        "uvx",
        "--from",
        "git+https://github.com/nishantapatil3/mcps#subdirectory=web-search-mcp",
        "web-search-mcp"
      ],
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

#### Claude Code

```bash
claude mcp add web-search -- \
  uvx --from 'git+https://github.com/nishantapatil3/mcps#subdirectory=web-search-mcp' web-search-mcp
```

#### Claude Desktop / Cursor / Windsurf

`claude_desktop_config.json` (and equivalents) use a `command` + `args` split:

```json
{
  "mcpServers": {
    "web-search": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/nishantapatil3/mcps#subdirectory=web-search-mcp",
        "web-search-mcp"
      ]
    }
  }
}
```

> Pin to a tag or commit for reproducibility, e.g.
> `git+https://github.com/nishantapatil3/mcps@v0.1.0#subdirectory=web-search-mcp`.
> Tracking the default branch means a push changes what your client executes.

### How it works

Engines are tried in order and the first one to return parseable results wins:

1. **Bing** (`GET /search`)
2. **DuckDuckGo** (`POST html.duckduckgo.com/html/`)

Blocks are detected by body content, not just status code — DuckDuckGo answers
rate-limited requests with `HTTP 202` and an "anomaly" interstitial rather than a
`4xx`. When an engine is skipped, the reason is reported in a `notes` field
alongside whatever results did come back.

Bing wraps result links in `bing.com/ck/a` redirects carrying the real target in
a base64 `u` parameter; DuckDuckGo proxies through `/l/?uddg=`. Both are unwrapped
so returned URLs are the real destinations. If either format changes, the
original URL is passed through rather than raising.

Tools return structured `{"error": ...}` payloads instead of raising, so a
transient network failure degrades one tool call rather than dropping the MCP
session.

### Caveats

This scrapes HTML from search engines that do not offer a keyless API. That
carries real consequences:

- **Rate limits are real.** DuckDuckGo begins challenging repeated requests from
  one IP quickly. Bing is more tolerant but not unlimited.
- **Markup changes break parsers.** Selectors target current HTML; a redesign
  means `web_search` returns `no results parsed` until updated.
- **Not for high volume.** For heavy or production use, prefer a real search API
  (Exa, Brave, Tavily) or a hosted remote MCP endpoint.

### Development

```bash
cd web-search-mcp
uv run --group dev pytest        # parsing/fallback tests, no network needed
uv run --group dev pytest -m network   # live end-to-end checks
```

Network-dependent tests are marked `network` and skipped by default so the suite
stays deterministic offline.

## License

MIT
