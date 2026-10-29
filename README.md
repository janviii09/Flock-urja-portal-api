# Urja Meter Ops API

A REST API wrapping the legacy **Urja Meter Ops** utility portal. The portal works fine for humans clicking around — it has no API, so this service handles auth, data extraction, caching, and exposes everything through clean JSON endpoints.

---

## Table of Contents

1. [What's Built](#whats-built)
2. [Project Structure](#project-structure)
3. [Installation & Setup](#installation--setup)
4. [Configuration (.env)](#configuration-env)
5. [Running the App](#running-the-app)
6. [API Endpoints](#api-endpoints)
7. [Design Decisions & Trade-offs](#design-decisions--trade-offs)
8. [Assumptions](#assumptions)
9. [What Was Skipped](#what-was-skipped)
10. [What I'd Improve](#what-id-improve)
11. [Scale Limitations](#scale-limitations)
12. [Key Documents](#key-documents)

---

## What's Built

The legacy portal is built for manual browser use without a public API. This service:

- Authenticates with the portal (handling SvelteKit's CSRF requirements and session expiry)
- Pulls data via the portal's internal JSON endpoints, including a bulk export that needs ephemeral HMAC-SHA256 signing
- Stores everything locally in SQLite with WAL mode and per-record provenance
- Serves it through a versioned FastAPI with cursor pagination, geospatial search, hierarchy tree traversal, and RFC 7807 error responses
- Keeps data fresh with tiered TTLs and single-flight request deduplication

---

## Project Structure

```
Flock-assign/
├── PROTOCOL.md                    # How the portal actually works (reverse-engineered)
├── REFLECTION.md                  # Reflection answers
├── openapi.json                   # OpenAPI 3.1.0 spec
├── Makefile
├── pyproject.toml
├── scripts/
│   └── export_openapi.py
└── src/urja/
    ├── adapters/                  # Portal JSON parsers — the only layer that knows portal wire shapes
    │   ├── export.py              # /portal/export bulk dump
    │   ├── search.py              # /portal/meters/search pagination
    │   ├── energy.py              # /portal/meters/{id}/energy, IST→UTC conversion
    │   └── dts.py                 # /portal/dts transformers
    ├── api/
    │   ├── app.py                 # FastAPI entry point, error handler registration
    │   ├── deps.py                # Dependency injection
    │   ├── errors.py              # RFC 7807 Problem Details
    │   └── v1/
    │       ├── router.py          # Route handlers
    │       └── schemas.py         # Response envelope schemas
    ├── client/
    │   ├── exceptions.py
    │   └── portal.py              # HMAC signing, SvelteKit CSRF, retry/backoff, session recovery
    ├── domain/models.py           # Pydantic v2 domain models
    ├── freshness/manager.py       # TTL policies, single-flight deduplication
    ├── hierarchy/tree.py          # Tree synthesis, cycle/multi-parent repair, quality audit
    ├── store/
    │   ├── repository.py          # SQLite queries, cursor pagination, haversine nearby
    │   └── schema.py              # DDL + 6 indexes
    ├── sync/
    │   ├── __main__.py            # CLI: python -m urja.sync
    │   └── engine.py              # Bulk export, energy sync, atomic transactions
    ├── logging.py                 # Secret-redacting log filter
    └── settings.py
```

Tests (103 total):

```
tests/
├── test_adapters.py      # Parser correctness and portal quirk handling
├── test_api.py           # End-to-end API with MockTransport, RFC 7807 checks
├── test_client.py        # Auth, session recovery, concurrency, retry
├── test_hardening.py     # Session expiry mid-sync, 502 containment, secret scrubbing
├── test_hierarchy.py     # Graph repair, recursive CTEs, spatial queries
├── test_phase1.py        # Domain model contracts
├── test_store.py         # Idempotent upserts, provenance, indexes
└── test_sync.py          # Multi-page sync, change detection, partial failure isolation
```

---

## Installation & Setup

Requires Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

Or just:

```bash
make install
```

---

## Configuration (.env)

```bash
cp .env.example .env
```

```ini
URJA_BASE_URL="https://urja-ops.flockenergy.tech"
URJA_EMAIL="operator@urja.local"
URJA_PASSWORD="urja-ops-2026"

DB_PATH="urja.db"

PORTAL_TIMEOUT=15.0
PORTAL_CONCURRENCY=4
PORTAL_MAX_RETRIES=3

LOG_LEVEL="INFO"
PORTAL_RECORD_CAPTURES=false
PORTAL_CAPTURES_DIR="captures"
```

---

## Running the App

**Sync data from the portal first:**

```bash
# Full sync: all 403 meters, hierarchy, and energy telemetry
python -m urja.sync

# Skip energy telemetry (faster)
python -m urja.sync --no-energy

# Sync energy for just the first N meters
python -m urja.sync --energy-limit 20
```

**Start the API:**

```bash
uvicorn urja.api.app:app --host 0.0.0.0 --port 8000 --reload
# or: make run
```

Once running:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

**Tests and lint:**

```bash
pytest
pytest -v
ruff check .
ruff format .
python scripts/export_openapi.py   # regenerate openapi.json
```

---

## API Endpoints

Every `/v1/*` response includes a `meta` envelope:

```json
"meta": {
  "fetched_at": "2026-10-29T13:45:00Z",
  "age_seconds": 12.4,
  "stale": false,
  "source": "store"
}
```

### Health

```bash
curl http://localhost:8000/v1/health
```

```json
{
  "status": "ok",
  "portal_reachable": true,
  "last_sync": {
    "started_at": "2026-10-29T13:00:00Z",
    "status": "success",
    "items_new": 403
  },
  "meta": { ... }
}
```

### List Meters

```bash
curl "http://localhost:8000/v1/meters?limit=20&phase=single&make=HPL"
```

Filters: `phase`, `make`, `status`, `dt_code`, `serial_no`. Cursor pagination via `cursor` param.

### Meter Detail

```bash
curl http://localhost:8000/v1/meters/J100000
```

Returns nameplate, GPS coordinates, and the full 7-level hierarchy path (Zone › Circle › Division › Subdivision › Substation › Feeder › DT).

### Consumption Time Series

```bash
curl "http://localhost:8000/v1/meters/J100000/consumption?from=2026-06-20T00:00:00Z&to=2026-06-23T23:30:00Z&limit=48"
```

30-minute interval readings. Preserves the portal's original `raw_timestamp` string alongside UTC-normalized `timestamp`.

```json
{
  "meter_id": "J100000",
  "raw_timestamp": "23/06/2026 23:30",
  "timestamp": "2026-06-23T18:00:00Z",
  "kwh": 48438.74,
  "kvah": 52313.84,
  "voltage_r": 226.0
}
```

### Network Hierarchy Tree

```bash
curl http://localhost:8000/v1/hierarchy
```

Reconstructs the full 7-level tree. Handles messy portal data: normalizes duplicate codes, breaks cycles, resolves meters with conflicting parent references.

### Subtree / Children

```bash
curl http://localhost:8000/v1/nodes/SS-01/subtree
curl http://localhost:8000/v1/nodes/Z-01/children
```

Subtree uses SQLite recursive CTEs with a depth guard at 20 levels.

### Nearby Meters

```bash
curl "http://localhost:8000/v1/meters/nearby?lat=26.9389&lon=75.8309&radius_km=2.0&limit=10"
```

Two-phase: SQL bounding-box prefilter then exact Haversine calculation. Excludes meters at (0, 0).

### Data Quality Report

```bash
curl http://localhost:8000/v1/quality
```

Audits hierarchy anomalies (orphans, cycles, multiple parents), meter issues (null island coordinates, unknown enums), and consumption anomalies (non-monotonic cumulative kWh).

### Errors

All 4xx/5xx responses return `application/problem+json` (RFC 7807):

```bash
curl -i "http://localhost:8000/v1/meters/INVALID ID"
```

```json
{
  "type": "https://urja-ops.flockenergy.tech/errors/validation-error",
  "title": "Unprocessable Entity",
  "status": 422,
  "detail": "The request parameters failed validation schema checks.",
  "instance": "/v1/meters/INVALID%20ID"
}
```

---

## Design Decisions & Trade-offs

**quality_flags instead of silent data cleaning** — when the portal has bad data (coordinates at (0, 0), non-monotonic readings, unknown status values), the record is kept but tagged. Nothing is silently dropped. Consumers can decide what to trust.

**SQLite with WAL mode** — no external infrastructure required. WAL gives concurrent reads without blocking. The trade-off is single-writer concurrency, which is fine here but would need a proper database for horizontal scaling.

**Single-flight deduplication** — if 20 concurrent requests all hit an expired cache entry at the same time, only one actually calls the portal. The other 19 wait on the same asyncio Future. Works great in a single process; would need Redis or similar for multi-replica deployments.

**Deterministic hierarchy repair** — the portal's hierarchy data has inconsistencies (same code appearing under multiple parents, some codes missing entirely). Rather than dropping bad nodes, the tree builder applies deterministic rules: normalize codes, pick the lexically first parent when there are duplicates, break cycles by detaching the offending edge. Everything ends up in the tree; the quality endpoint shows what got repaired.

**Full-path keying for hierarchy nodes** — node codes like `SD-01` can appear under different parents (different zones have their own `SD-01`). Keying by full path (`Z-01/C-01/D-01/SD-01`) instead of bare code handles this cleanly.

**Cursor pagination** — offset-based pagination is unstable (inserts shift results mid-page). Cursor pagination (`meter_id > last_seen`) gives stable, O(1) page access.

---

## Assumptions

- The portal is read-only. The client enforces this at the transport layer — non-GET requests are rejected before they're sent.
- Portal timestamps are unzoned and assumed to be IST (UTC+05:30), since this is an Indian electricity utility. They're converted to UTC for storage and querying, with the original string preserved as `raw_timestamp`.
- The 7-level breadcrumb on meter detail pages is the authoritative source of network hierarchy. There's no dedicated hierarchy endpoint.
- Consumption readings are cumulative registers (kWh meter total), not interval deltas. The quality report flags any reading where the cumulative value decreases.

---

## What Was Skipped

**Consumer authentication** — endpoints are open. The assignment was about wrapping the upstream portal, and adding JWT/API keys would make local setup more annoying for reviewers. In production this would be the first thing to add.

**Real-time push** — there's no SSE or WebSocket endpoint for sync events. The `/v1/health` endpoint shows the last sync run, which is enough for a pull-based client.

**Distributed rate limiting** — the semaphore protecting the portal is in-process. For a multi-replica deployment this would need Redis.

---

## What I'd Improve

- **Auth on the API** — API key or OAuth2 bearer tokens with RBAC (Admin, Operator, Read-Only).
- **Redis for distributed coordination** — replace in-process SingleFlight and semaphores with Redlock.
- **PostgreSQL + PostGIS** — for anything beyond district scale. The spatial bounding-box + Haversine approach works fine for 403 meters but would degrade at millions. PostGIS `ST_DWithin` with a GIST index handles it properly.
- **PostgreSQL ltree for hierarchy** — materializing the hierarchy path in the database means subtree queries become O(1) prefix scans instead of recursive CTEs.
- **Background sync daemon** — currently sync only runs on-demand (`python -m urja.sync`). A periodic background worker (Celery or APScheduler) would keep data continuously fresh.
- **Dockerfile** — straightforward to add, just didn't prioritize it.

---

## Scale Limitations

The current approach is sized for district-level data (hundreds of meters, tens of hierarchy nodes). It works fine for this dataset.

Where it would start to struggle:

- **Spatial queries** — SQLite has no multi-dimensional spatial index. The bounding-box prefilter uses a B-tree scan over lat/lon columns. Fine at 403 meters, slow at millions.
- **Recursive CTE memory** — SQLite materializes working tables in temp files for recursive queries. Large hierarchies would hit I/O.
- **In-memory tree synthesis** — building the entire hierarchy tree in Python memory is instant for hundreds of nodes, but creates GC pressure at nation scale.

For a production utility grid: PostgreSQL + PostGIS for spatial, `ltree` extension for hierarchy, or push hierarchy into Neo4j.

---

## Key Documents

- [PROTOCOL.md](./PROTOCOL.md) — How the portal actually works, from direct observation.
- [REFLECTION.md](./REFLECTION.md) — Reflection questions answered.
- [openapi.json](./openapi.json) — Full OpenAPI 3.1.0 schema.
