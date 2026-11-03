# fgelev Docker service

A Docker image that runs [fgelev](https://wiki.flightgear.org/Fgelev) as a
persistent TCP service. fgelev is a FlightGear utility that queries terrain
elevation for a given longitude/latitude coordinate using the same scenery
data the simulator uses.

## Overview

The image builds fgelev from the FlightGear 2020.3 release (SimGear + a
minimal FlightGear build with only the fgelev target enabled). At runtime a
Python proxy wraps fgelev and exposes it on TCP port 9000.

### Why a proxy?

fgelev communicates over stdin/stdout. A direct TCP wrapper (e.g. socat) would
spawn a new fgelev process per connection, discarding its terrain-tile cache on
every disconnect. The proxy keeps **one fgelev process alive** for the lifetime
of the container, so the cache warms up once and stays warm across all export
runs. It also multiplexes concurrent client connections — fgelev's protocol
echoes a caller-supplied id on each response, so multiple clients can have
queries in flight simultaneously.

If fgelev exits unexpectedly the proxy restarts it automatically after a short
delay.

### Protocol

The protocol is unchanged from the fgelev binary:

```
client → service:  "<id> <lon> <lat>\n"
service → client:  "<id>:<elevation>\n"
```

- `id` — an arbitrary integer supplied by the client, echoed back verbatim
- `lon`, `lat` — decimal degrees
- `elevation` — metres above MSL; `-9999` if no terrain data is available

## Building

```bash
docker build -t fgelev:latest .
```

The build is a two-stage Docker build. The first stage compiles SimGear as
static libraries and then builds only the fgelev binary from FlightGear. The
second stage is a lean runtime image with only the OSG runtime libraries and
Python 3.

> **Note:** The build clones FlightGear and SimGear from SourceForge and
> compiles them from source. This takes 10–20 minutes depending on hardware.

## Configuration

The proxy and fgelev are configured via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `FG_ROOT` | Path to FlightGear data root inside the container (contains `defaults.xml`) | — (required) |
| `FG_SCENERY` | Path to terrain scenery inside the container | — (required) |
| `FGELEV_PATH` | Path to the fgelev binary inside the container | `/usr/local/bin/fgelev` |
| `OSG_NOTIFY_LEVEL` | OSG log verbosity (`WARN` suppresses plugin-loading noise) | `WARN` |

The proxy itself accepts two optional command-line arguments (passed via
`command:` in Compose):

| Argument | Description | Default |
|----------|-------------|---------|
| `--port` | TCP port to listen on | `9000` |
| `--host` | Interface to bind to | `0.0.0.0` |
| `--log-level` | Proxy log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |

## Docker Compose

The service requires two read-only volume mounts: the FlightGear data root
(`FG_ROOT`) and the terrain scenery (`FG_SCENERY`). Both must be provided by
the host.

```yaml
services:
  fgelev:
    build:
      context: ./export/fgelev
    image: fgelev:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:9000:9000"   # bind to localhost only — no authentication
    volumes:
      - /path/to/fg/data:/fg/root:ro       # FG_ROOT
      - /path/to/fg/scenery:/fg/scenery:ro # FG_SCENERY (must contain Terrain/)
    environment:
      FG_ROOT: /fg/root
      FG_SCENERY: /fg/scenery
```

To enable debug logging from the proxy:

```yaml
    command: ["python3", "/proxy.py", "--log-level", "DEBUG"]
```

### Using with fgsmdb

Point fgsmdb at the service via `.env`:

```ini
FGELEV_TCP_HOST=fgelev   # service name in Compose, or hostname/IP
FGELEV_TCP_PORT=9000     # default, can be omitted
```

`FG_ROOT` and `FG_SCENERY` are not needed in fgsmdb's `.env` when using TCP
mode — terrain data is configured on the service side only.

Full example with fgsmdb in the same Compose stack:

```yaml
services:
  fgelev:
    build:
      context: ./export/fgelev
    image: fgelev:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:9000:9000"
    volumes:
      - /path/to/fg/data:/fg/root:ro
      - /path/to/fg/scenery:/fg/scenery:ro
    environment:
      FG_ROOT: /fg/root
      FG_SCENERY: /fg/scenery

  exporter:
    image: python:3.13-slim
    working_dir: /app
    volumes:
      - ./.env:/app/.env:ro
      - /path/to/fg/scenery:/fg/scenery:ro
      - /path/to/fg/scenery-export:/fg/export
    environment:
      FGELEV_TCP_HOST: fgelev
      FGELEV_TCP_PORT: "9000"
    command: >
      sh -c "pip install --quiet fgsmdb && python -m fgsmdb export --env-file /app/.env"
    depends_on:
      - fgelev
```

## Security

The service has no authentication. Bind the port to `127.0.0.1` (or an
internal Docker network) rather than exposing it publicly — anyone who can
reach port 9000 can query elevations and keep the fgelev process busy.

## Terrain data

The `FG_SCENERY` mount must contain a `Terrain/` subdirectory with
TerraSync-format terrain data. fgelev returns `-9999` for coordinates where
no terrain tile is available (ocean, or areas not covered by your local
scenery).

The terrain tile cache is held in fgelev's process memory and persists for
the lifetime of the container. The first query for a new tile area is slower
while the tile is loaded; subsequent queries for the same area are fast.

