# Urja Meter Ops API

A clean REST API wrapping the legacy **Urja Meter Ops** portal, built for
Flock Energy. The portal has no API — this service scrapes, normalises, caches,
and re-exposes meter data, consumption readings, transformers, and the network
hierarchy through a documented JSON API.

## Project Structure

```
src/urja/
├── api/          # FastAPI /v1 endpoints
├── adapters/     # Portal response parsers (only layer that knows portal format)
├── client/       # HTTP session management (auth, retry, rate limit)
├── domain/       # Pydantic models, enums, quality_flags
├── store/        # SQLite persistence with provenance
├── sync/         # Bulk + incremental data sync
├── freshness/    # TTL, stale-while-revalidate, single-flight
├── hierarchy/    # Network tree reconstruction
├── settings.py   # Typed config from env vars
└── logging.py    # Centralised logging setup
```

## Quick Start

```bash
# 1. Clone and install
git clone <repo-url> && cd urja-api
cp .env.example .env          # fill in real credentials
make install                  # pip install -e ".[dev]"

# 2. Run
make run                      # uvicorn on :8000

# 3. Test
make test                     # pytest -v

# 4. Lint
make lint                     # ruff check + format
```

## Sample Request

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

_(More endpoints coming in subsequent phases.)_

## Key Documents

- [PROTOCOL.md](./PROTOCOL.md) — How the portal actually works (data, auth, quirks)
- [DECISIONS.md](./DECISIONS.md) — Trade-offs, assumptions, and mistakes log
- `openapi.json` — Generated OpenAPI spec (`make openapi`)

## Design Decisions & Trade-offs

See [DECISIONS.md](./DECISIONS.md) for the full log. Key choices:

- **quality_flags over silent cleaning**: Messy data is flagged, never silently fixed.
- **Adapter isolation**: Only `adapters/` knows the portal's HTML/data format.
- **SQLite with provenance**: Every stored record tracks when/where/what was fetched.
- **Credentials via env only**: No secrets in code or config files.

## Assumptions

- Export button produces CSV with all meters (format unconfirmed).
- Search on `/meters` is server-side (not client-side JS filtering).
- Readings shown are latest ~15 entries per meter.

## What I'd Improve

- Add authentication to the API itself (JWT or API keys).
- Implement WebSocket for live sync status.
- Add structured OpenTelemetry tracing.
- Containerise with multi-stage Docker build.
- Load test against the mock portal.

## Stack

Python 3.9+ · FastAPI · httpx · Pydantic v2 · SQLite · pytest · ruff
