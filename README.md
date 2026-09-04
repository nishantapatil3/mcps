# mcps

Model Context Protocol servers, runnable directly with `uvx` — no cloning, no
global installs, no Docker.

| Server | Description |
| --- | --- |
| [`web-search-mcp`](./web-search-mcp) | Web search + page fetch over plain HTTP. No browser, no API key. |

## Quick start

Add to OpenCode (`~/.config/opencode/opencode.json`):

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

Or Claude Code:

```bash
claude mcp add web-search -- \
  uvx --from 'git+https://github.com/nishantapatil3/mcps#subdirectory=web-search-mcp' web-search-mcp
```

See each server's README for full configuration and caveats.

## License

MIT
