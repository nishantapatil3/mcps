# location-tools

MCP server for location questions: where am I, where is *X*, what time is it
there, and how far apart are they. Runs over plain HTTPS against free community
services — **no API key, no browser, no account**.

```
current_location   approximate location of the host (configured or IP-derived)
location_details   full details for a place, address, or coordinate pair
geocode            place name -> ranked coordinate candidates
reverse_geocode    coordinates -> nearest address
location_timezone  IANA timezone, UTC offset, and current local time
ip_location        geolocate a specific IP address
distance           great-circle distance + compass bearing between two points
```

## Read this before trusting `current_location`

**This server cannot know where you are.** It has no GPS and no access to your
device's location. Absent configuration, it geolocates the **public IP of the
machine running the server**, which means:

- City-level accuracy *at best*, and frequently tens of km off.
- Behind a VPN it reports the exit node — potentially the wrong continent.
- On carrier/CGNAT networks it reports the ISP's aggregation point, not you.
- Some providers answer an unknown prefix with the **country's centroid**, which
  looks like a real coordinate but is not one.

Those failure modes are not hypothetical. Querying three providers for one
residential IP during development returned three different cities, the furthest
pair ~70 km apart.

So `current_location` does two things instead of pretending to certainty: it
queries several providers and reports their **spread and agreement level**, and
it labels every response with a `source` (`configured` or `ip_geolocation`) plus
an `accuracy_note`. If you need a position that is actually right, state it:

```bash
location-tools --home "48.8584,2.2945" --home-label "Home, Paris"
```

`--home` also accepts a place name (`--home "Kyoto, Japan"`), geocoded once at
startup. With it set, `current_location` returns that position and stops guessing.

## Install

Run straight from the repo with no clone:

```bash
uvx --from 'git+https://github.com/nishantapatil3/mcps#subdirectory=location-tools' location-tools
```

### OpenCode

`~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "location-tools": {
      "type": "local",
      "command": [
        "uvx",
        "--from",
        "git+https://github.com/nishantapatil3/mcps#subdirectory=location-tools",
        "location-tools",
        "--home",
        "37.7749,-122.4194"
      ],
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

### Claude Code

```bash
claude mcp add location-tools -- \
  uvx --from 'git+https://github.com/nishantapatil3/mcps#subdirectory=location-tools' \
  location-tools --home '37.7749,-122.4194'
```

## Tools

### `current_location`

Approximate location of the host: coordinates, city, region, country, timezone.
Set `include_address: true` to also reverse geocode to a street address (usually
pointless — the coordinates are rarely precise enough for the street to mean
anything).

Always carries `source` and `accuracy_note`. When IP-derived it also includes
`agreement` (`close` / `loose` / `poor` / `single-provider`),
`provider_spread_km`, and the individual `providers` array.

### `location_details`

Resolves a place name, address, or `lat,lon` to full details — coordinates,
address components, bounding box, timezone. Omit the argument to describe the
current location.

### `geocode`

Place name or address to ranked coordinate candidates. Returns a list, because
"Springfield" and "Cambridge" genuinely are ambiguous and silently taking the
top hit is how you end up in the wrong country. `country_codes: "us"` narrows it.

### `reverse_geocode`

Coordinates to nearest address, with components (road, city, state, postcode,
country). `zoom` controls granularity: 18 building, 10 city, 5 state.

### `location_timezone`

IANA timezone, UTC offset, and current local time for a place or coordinate pair.
Omit the location to use the current one.

### `ip_location`

Geolocates an explicit IP. Separate from `current_location` so "locate this
host" and "locate this address" can't be confused for one another. Same
multi-provider agreement reporting.

### `distance`

Great-circle distance and initial compass bearing. Each endpoint is a place name
or coordinates; omit `origin` to measure from the current location. `unit` is
`km`, `mi`, or `nmi`.

**This is not driving distance.** It ignores roads, terrain, and borders — a
straight line over a sphere. For a city pair it typically reads 20–30% short of
the actual drive.

## Configuration

Every setting has a CLI flag and an env var. Flags win.

| Flag | Env var | Default | Purpose |
| --- | --- | --- | --- |
| `--home` | `LOCATION_TOOLS_HOME` | unset | Fixed position. Flag takes `lat,lon` or a place name; the env var takes coordinates only. |
| `--home-label` | `LOCATION_TOOLS_HOME_LABEL` | unset | Human-readable name for `--home`. |
| `--user-agent` | `LOCATION_TOOLS_USER_AGENT` | `location-tools-mcp/<version>` | Sent upstream; Nominatim's policy requires identification. |
| `--nominatim-url` | `LOCATION_TOOLS_NOMINATIM_URL` | `https://nominatim.openstreetmap.org` | Point at a self-hosted instance. |
| `--language` | `LOCATION_TOOLS_LANGUAGE` | `en` | Language for place names. Without it Nominatim answers in the local language of whatever you queried — `日本` rather than `Japan`. |
| `--requests-per-minute` | `LOCATION_TOOLS_REQUESTS_PER_MINUTE` | `60` | Self-imposed throttle. |
| `--cache-ttl` | `LOCATION_TOOLS_CACHE_TTL` | `900` | Seconds to cache geocodes; `0` disables. |
| `--ca-certs` | `LOCATION_TOOLS_CA_CERTS` | unset | CA bundle for TLS-intercepting proxies. |
| `--no-ssl-verify` | `LOCATION_TOOLS_SSL_VERIFY=0` | verify on | Disable TLS verification. Insecure. |

The env var is the better channel for `--home` when the position is sensitive: a
CLI flag is visible in `ps` output to every user on the machine.

**Caveat on env vars with MCP clients.** The MCP stdio launcher passes only a
safe-list of variables (`HOME`, `PATH`, `SHELL`, `TERM`, `USER`, `LOGNAME`) to
the server process — your shell's `LOCATION_TOOLS_*` exports do **not** reach it
unless the client is configured to forward them. If a setting appears to be
ignored, that is why. Either use the CLI flag, or declare the variable in the
client's own config:

```json
{
  "mcp": {
    "location-tools": {
      "type": "local",
      "command": ["uvx", "--from", "git+https://github.com/nishantapatil3/mcps#subdirectory=location-tools", "location-tools"],
      "environment": { "LOCATION_TOOLS_HOME": "48.8584,2.2945" },
      "enabled": true
    }
  }
}
```

The startup banner on stderr reports the resolved home position, so you can
confirm which channel won.

## Upstream services and their rules

| Service | Used for | Notes |
| --- | --- | --- |
| [Nominatim](https://nominatim.openstreetmap.org) | forward + reverse geocoding | OSM data, ODbL. [Usage policy](https://operations.osmfoundation.org/policies/nominatim/): max 1 req/sec, identifying User-Agent required, caching expected. |
| [Open-Meteo](https://open-meteo.com) | timezone resolution | Keyless; non-commercial use. |
| [ipwho.is](https://ipwho.is), [ipinfo.io](https://ipinfo.io), [geojs.io](https://geojs.io) | IP geolocation | Keyless tiers, all HTTPS. |

This server ships a compliant User-Agent, a sliding-window rate limiter, and a
TTL cache for exactly those reasons — the public Nominatim instance blocks
misbehaving clients, and the block outlasts any single session.

`ip-api.com` is deliberately **not** used despite being the most popular free
option: its keyless tier is HTTP-only, so the lookup and its answer would cross
the network in plaintext, letting anyone on-path rewrite the coordinates this
server then reports as your location.

Geocoding results are `© OpenStreetMap contributors`, licensed ODbL. Attribute
accordingly if you display them.

## Development

```bash
uv sync --group dev
uv run pytest              # offline suite, no network
uv run pytest -m network   # live checks against the real services
```

The offline suite stubs the HTTP layer at `fetch._build_client`, so tests are
deterministic and never touch the upstream services. Live tests are opt-in via
the `network` marker.

## License

MIT
