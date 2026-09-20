# mcps

Model Context Protocol servers, runnable directly with `uvx` — no cloning, no
global installs, no Docker.

| Server | Description |
| --- | --- |
| [`web-tools`](./web-tools) | Web search + page fetch over plain HTTP. No browser, no API key. Optional Chrome TLS impersonation for bot-filtered sites; SSRF-guarded fetching. |

## Quick start

Add to OpenCode (`~/.config/opencode/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "web-tools": {
      "type": "local",
      "command": [
        "uvx",
        "--from",
        "git+https://github.com/nishantapatil3/mcps#subdirectory=web-tools",
        "web-tools"
      ],
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

Or Claude Code:

```bash
claude mcp add web-tools -- \
  uvx --from 'git+https://github.com/nishantapatil3/mcps#subdirectory=web-tools' web-tools
```

See each server's README for full configuration and caveats.

## License

MIT
