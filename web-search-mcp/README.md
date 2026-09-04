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

`web_search` takes `query`, plus optional `limit` (1–50, default 10), `region`
(`us-en`, `de-de`, `jp-ja`, `wt-wt` for none), and `safe_search`
(`strict` / `moderate` / `off`).

`fetch_page` takes `url`, plus optional `max_chars` (200–50000, default 8000),
`start_index` (pagination offset), and `backend` (`httpx` / `curl` / `auto`).
When a page runs longer than the window, the result carries `total_length` and a
`next_start_index` to pass back on the following call.

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

To install the optional Chrome TLS impersonation backend at the same time, add
`--with 'curl_cffi>=0.7'` to the `uvx` invocation (before `--from`). See
[Getting past bot filters](#getting-past-bot-filters).

> Pin to a tag or commit for reproducibility, e.g.
> `git+https://github.com/nishantapatil3/mcps@v0.1.0#subdirectory=web-search-mcp`.
> Tracking the default branch means a push changes what your client executes.

### How it works

Search goes to a single engine, **DuckDuckGo** (`POST html.duckduckgo.com/html/`).

Bing used to be tried first and was removed. It answered a plain client with a
SERP that ignored every term after the first: `python list comprehension
performance` returned the Python.org homepage, and `rust async runtime tokio
benchmarks` returned the Rust video game. The response was HTTP 200 with
well-formed `li.b_algo` markup and ten parseable results, so nothing in the block
detection could see it, and being first in the chain it silently shadowed the
engine that worked. Sending Bing's own SERP parameters, an `SRCHHPGUSR` cookie,
and `form=QBLH` changed nothing.

Blocks are detected from the status **and** the body, because the interesting
ones are all `2xx`. DuckDuckGo answers a TLS fingerprint it dislikes with an
empty `HTTP 202`; Cloudflare serves its interstitial at `HTTP 200`. Neither trips
`raise_for_status`, so both would otherwise parse to zero results with nothing to
explain why. When an engine is skipped or retried, the reason is reported in a
`notes` field alongside whatever results did come back.

DuckDuckGo proxies result links through `/l/?uddg=` and routes sponsored results
through `/y.js`. Wrappers are unwrapped so returned URLs are real destinations,
and ads are dropped. If the format changes, the original URL is passed through
rather than raising.

Tools return structured `{"error": ...}` payloads instead of raising, so a
transient network failure degrades one tool call rather than dropping the MCP
session.

### Getting past bot filters

Modern filters block on **TLS fingerprint**, which no amount of header tuning
fixes. The optional `browser` extra pulls in [`curl_cffi`][curl_cffi], which
performs a real Chrome TLS handshake:

```bash
uvx --with 'curl_cffi>=0.7' \
  --from 'git+https://github.com/nishantapatil3/mcps#subdirectory=web-search-mcp' \
  web-search-mcp
```

[curl_cffi]: https://github.com/lexiforest/curl_cffi

Three backends are selectable, for search and for fetching independently:

| Backend | Behavior |
| --- | --- |
| `httpx` | Plain client only. Fast, no extra dependency, blockable. |
| `curl` | Chrome TLS impersonation on every request. Requires the extra. |
| `auto` | **Default.** Try `httpx`; retry through `curl` only on a block signal. |

Under `auto` the retry costs nothing when nothing is blocked, and the fallback is
skipped entirely when `curl_cffi` is absent — with the install hint surfaced in
the error, so a persistent block is diagnosable rather than mysterious.

### Fetching is guarded

`fetch_page` follows URLs the model chose, which may have come from a search
result or a page it just read. Unguarded, that is a confused deputy: anything the
host can reach on its own network becomes readable through a tool call.

So `fetch_page` resolves the target and refuses any URL landing on a loopback,
private, CGNAT, link-local (including the `169.254.169.254` cloud metadata
endpoint), reserved, multicast, or unspecified address. Redirects are followed by
hand, up to five hops, and **every hop is re-validated** — a public URL cannot
bounce the fetch inward. Pass `--allow-private-urls` for a trusted local
deployment that intentionally reads internal hosts.

Both tool descriptions also tell the model that returned text is untrusted input,
since page content is exactly where a prompt injection lives.

### Configuration

Every setting has a CLI flag and an environment variable; the flag wins. These
are startup configuration, deliberately not tool parameters — the model can pick
a region or a per-call fetch backend, but not disable the SSRF guard.

| Flag | Env var | Default |
| --- | --- | --- |
| `--search-backend` | `WEB_SEARCH_BACKEND` | `auto` |
| `--fetch-backend` | `WEB_SEARCH_FETCH_BACKEND` | `auto` |
| `--safe-search` | `WEB_SEARCH_SAFE_SEARCH` | `moderate` |
| `--region` | `WEB_SEARCH_REGION` | none |
| `--allow-private-urls` | `WEB_SEARCH_ALLOW_PRIVATE_URLS` | off (guard on) |
| `--ca-certs PATH` | `WEB_SEARCH_CA_CERTS` | system trust store |
| `--no-ssl-verify` | `WEB_SEARCH_SSL_VERIFY=0` | verification on |
| `--requests-per-minute` | `WEB_SEARCH_REQUESTS_PER_MINUTE` | `30` |

`--ca-certs` matters behind a TLS-intercepting corporate proxy: httpx does not
read `SSL_CERT_FILE`, so the bundle has to be handed to it explicitly. Prefer it
over `--no-ssl-verify`.

The rate limit is self-imposed and shared across calls. Search endpoints start
challenging a single IP well before they start returning errors, so throttling
keeps the server usable for longer than retrying into a block would.

### Caveats

This scrapes HTML from search engines that do not offer a keyless API. That
carries real consequences:

- **Rate limits are real.** DuckDuckGo begins challenging repeated requests from
  one IP quickly, and it is now the only engine, so there is no second engine to
  fall through to. The `browser` extra raises the ceiling; it does not remove it.
- **Markup changes break parsers.** Selectors target current HTML; a redesign
  means `web_search` returns `no results parsed` until updated.
- **The SSRF guard has a TOCTOU seam.** The host is resolved for validation and
  resolved again by the client to connect, so an attacker controlling DNS could
  rebind between the two. Pinning the socket to the validated IP is out of scope;
  default-deny plus per-hop validation closes the practical vectors.
- **Not for high volume.** For heavy or production use, prefer a real search API
  (Exa, Brave, Tavily) or a hosted remote MCP endpoint.

### Development

```bash
cd web-search-mcp
uv run --group dev pytest              # offline: parsing, fallback, backends, SSRF guard
uv run --group dev pytest -m network   # live end-to-end checks

# same live checks with the curl backend available
uv run --group dev --with 'curl_cffi>=0.7' pytest -m network
```

Network-dependent tests are marked `network` and skipped by default so the suite
stays deterministic offline. An autouse fixture pins the offline suite off both
DNS and the curl backend, so no test can quietly escape the mock transport.

### Credits

The bot-filter handling, SSRF guard, and pagination follow the approach taken by
[nickclyde/duckduckgo-mcp-server](https://github.com/nickclyde/duckduckgo-mcp-server),
adapted to this server's multi-engine fallback and structured tool results.

## License

MIT
