# Urja Meter Ops API

A robust, production-grade REST API wrapping the legacy **Urja Meter Ops** utility web portal (`https://urja-ops.flockenergy.tech`).

The legacy portal provides no public API and is strictly read-only. This service authenticates, scrapes, normalizes, caches, and exposes smart meter metadata, 30-minute interval consumption time series, distribution transformers, network hierarchy trees, and geospatial queries through a clean, self-documenting JSON API with cache freshness provenance and RFC 7807 error handling.

---

## Table of Contents

1. [Architecture & Project Structure](#architecture--project-structure)
2. [Prerequisites & Installation](#prerequisites--installation)
3. [Configuration (.env)](#configuration-env)
4. [Running the Application](#running-the-application)
5. [Running Tests & Quality Checks](#running-tests--quality-checks)
6. [API Endpoints & Sample Requests](#api-endpoints--sample-requests)
7. [Design Decisions & Trade-offs](#design-decisions--trade-offs)
8. [Assumptions](#assumptions)
9. [What Was Skipped and Why](#what-was-skipped-and-why)
10. [What I'd Improve with More Time](#what-id-improve-with-more-time)
11. [Data Scale Limitations](#data-scale-limitations)
12. [Key Documents](#key-documents)

---

## Architecture & Project Structure

The project follows a layered domain-driven design where portal-specific quirks are strictly isolated from the rest of the application:

```
Flock-assign/
├── PROTOCOL.md                       # Reverse-engineered facts about the legacy portal
├── DECISIONS.md                      # Dated register of architectural choices, trade-offs, and mistakes
├── REFLECTION.md                     # Personal reflection notes drafted from project decisions
├── openapi.json                      # Exported OpenAPI 3.1.0 specification
├── Makefile                          # Standard commands (install, run, test, lint, openapi)
├── pyproject.toml                    # Poetry/setuptools package configuration and dependencies
├── scripts/
│   └── export_openapi.py             # Script to regenerate openapi.json from the FastAPI app
├── src/urja/
│   ├── adapters/                     # Verified portal JSON parsers (only layer aware of portal wire shape)
│   │   ├── export.py                 # Verified /portal/export bulk dump parser (path-based hierarchy, quality flags)
│   │   ├── search.py                 # Verified /portal/meters/search paginated search parser
│   │   ├── energy.py                 # Verified /portal/meters/{id}/energy parser (UTC conversion, raw_ts preservation)
│   │   └── dts.py                    # Verified /portal/dts?page=N distribution transformer parser
│   ├── api/                          # FastAPI presentation layer
│   │   ├── app.py                    # Application entry point and RFC 7807 error handler registration
│   │   ├── deps.py                   # Dependency injection providers (Store, Client, Freshness, Sync)
│   │   ├── errors.py                 # RFC 7807 Problem Details schemas and exception mappers
│   │   └── v1/
│   │       ├── router.py             # Version 1 route endpoints
│   │       └── schemas.py            # Pydantic response envelopes and pagination schemas
│   ├── client/                       # HTTP communication with the upstream portal
│   │   ├── exceptions.py             # Portal error hierarchy (AuthenticationError, PortalShapeError, etc.)
│   │   └── portal.py                 # Ephemeral HMAC-SHA256 signing, SvelteKit CSRF, concurrency limit, retry/backoff
│   ├── domain/                       # Pure domain models (Pydantic v2)
│   │   └── models.py                 # Meter, Reading, Transformer, HierarchyNode, QualityReport, etc.
│   ├── freshness/                    # Cache freshness engine
│   │   └── manager.py                # SingleFlight request deduplication, tiered TTLs, ResponseMeta
│   ├── hierarchy/                    # Network topological analysis
│   │   └── tree.py                   # Full-path tree synthesis, multi-parent detection, and defect auditing
│   ├── store/                        # Local persistence layer
│   │   ├── repository.py             # SQLite Store with WAL mode, change detection, and recursive CTEs
│   │   └── schema.py                 # SQLite DDL with 6 justified query indexes
│   ├── sync/                         # Data ingestion engine
│   │   ├── __main__.py               # CLI entrypoint: python -m urja.sync
│   │   └── engine.py                 # Verified bulk export, energy ingestion, atomic transactions
│   ├── logging.py                    # Logging setup with SecretRedactingFilter
│   └── settings.py                   # Pydantic Settings configuration from environment variables
└── tests/                            # Comprehensive test suite (103 tests)
    ├── fixtures/                     # Sanitized portal HTML and JSON captures
    ├── test_adapters.py              # Parsing, schema enforcement, and portal quirk tests
    ├── test_api.py                   # End-to-end API tests with MockTransport and RFC 7807 checks
    ├── test_client.py                # Auth, session recovery, concurrency, timeout, and retry tests
    ├── test_hardening.py             # Mid-sync session expiry, 502 containment, secret scrubbing
    ├── test_hierarchy.py             # Graph defect repair, recursive CTEs, and spatial query tests
    ├── test_phase1.py                # Contract and model verification tests
    ├── test_store.py                 # Idempotent upserts, provenance, and indexing verification
    └── test_sync.py                  # Multi-page sync, change detection, and partial failure isolation
```

---

## Prerequisites & Installation

- **Python**: `3.9` or newer (tested on Python 3.9.6 macOS).
- **Package Manager**: `pip` or virtualenv.

### 1. Create and Activate Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install --upgrade pip
pip install -e ".[dev]"
```

Alternatively, use the Makefile:
```bash
make install
```

---

## Configuration (.env)

Create a `.env` file in the project root by copying the template:

```bash
cp .env.example .env
```

Configure your parameters:

```ini
# Portal Credentials (Required for live portal sync)
URJA_BASE_URL="https://urja-ops.flockenergy.tech"
URJA_EMAIL="operator@flockenergy.tech"
URJA_PASSWORD="your-secure-password"

# Persistence
DB_PATH="urja.db"

# HTTP Client Tuning
PORTAL_TIMEOUT=15.0
PORTAL_CONCURRENCY=4
PORTAL_MAX_RETRIES=3

# Logging & Debugging
LOG_LEVEL="INFO"
PORTAL_RECORD_CAPTURES=false
PORTAL_CAPTURES_DIR="captures"
```

---

### Synchronize Data from Portal

To populate or refresh local SQLite data from the live portal:

```bash
# Full sync: bulk export (403 meters + 7-level hierarchy) and energy telemetry
python -m urja.sync

# Fast sync: bulk export only (skip energy telemetry)
python -m urja.sync --no-energy

# Targeted sync: bulk export + sample meters' energy telemetry
python -m urja.sync --energy-limit 20
```

### Start the API Server

```bash
uvicorn urja.api.app:app --host 0.0.0.0 --port 8000 --reload
```

Or via Makefile:
```bash
make run
```

Interactive documentation will be available at:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`
- **Raw OpenAPI JSON**: `http://localhost:8000/openapi.json`

---

## Running Tests & Quality Checks

The test suite includes **103 tests** across unit, integration, and mock-transport boundaries:

```bash
# Run all tests
pytest

# Run tests with detailed output
pytest -v

# Run lint and formatting checks
ruff check .
ruff format --check .

# Automatically apply formatting
ruff format .

# Re-export OpenAPI spec to openapi.json
python scripts/export_openapi.py
```

---

## API Endpoints & Sample Requests

Every `/v1/*` response is wrapped in an envelope containing standard cache freshness and origin provenance metadata:
```json
"meta": {
  "fetched_at": "2026-10-29T13:45:00Z",
  "age_seconds": 12.4,
  "stale": false,
  "source": "store"
}
```

### 1. Health & Upstream Connectivity

Check API health, portal reachability, and recent sync audit metadata:

```bash
curl -s http://localhost:8000/v1/health | jq
```

*Sample Response:*
```json
{
  "status": "ok",
  "portal_reachable": true,
  "last_sync": {
    "run_id": "8f8b3c91-9e6b-4c2d-8b01-574d75f284b1",
    "started_at": "2026-10-29T13:00:00Z",
    "completed_at": "2026-10-29T13:00:14Z",
    "status": "success",
    "resource": "meters_list",
    "items_new": 403,
    "items_changed": 0,
    "items_unchanged": 0,
    "items_failed": 0
  },
  "meta": {
    "fetched_at": "2026-10-29T13:45:00Z",
    "age_seconds": 0.0,
    "stale": false,
    "source": "store"
  }
}
```

---

### 2. List Smart Meters (Cursor Pagination & Filtering)

Retrieve meters filtered by manufacturer, phase, or DT with stable $O(1)$ cursor pagination:

```bash
curl -s "http://localhost:8000/v1/meters?limit=2&phase=three&make=Secure" | jq
```

*Sample Response:*
```json
{
  "data": [
    {
      "meter_id": "J100002",
      "serial_no": "SE34001",
      "make": "Secure",
      "phase_type": "three",
      "installation_status": "Installed",
      "installation_type": "Whole Current",
      "dt_code": "DT-001",
      "location": {
        "latitude": 26.9389,
        "longitude": 75.8309,
        "quality_flags": []
      },
      "quality_flags": []
    }
  ],
  "pagination": {
    "next_cursor": "J100002",
    "has_more": true,
    "total": 142
  },
  "meta": {
    "fetched_at": "2026-10-29T13:00:00Z",
    "age_seconds": 2700.0,
    "stale": false,
    "source": "store"
  }
}
```

---

### 3. Get Meter Detail (Stale-While-Revalidate)

Retrieve full nameplate, GPS coordinates, and 7-level hierarchy path (`Zone › Circle › Division › Subdivision › Substation › Feeder › DT`). If stale, triggers a background single-flight refresh while serving existing store data:

```bash
curl -s http://localhost:8000/v1/meters/J100000 | jq
```

*Sample Response:*
```json
{
  "data": {
    "meter_id": "J100000",
    "serial_no": "SE33962",
    "make": "HPL",
    "phase_type": "single",
    "installation_status": "Decommissioned",
    "installation_type": "Whole Current",
    "dt_code": "DT-001",
    "location": {
      "latitude": 26.938961,
      "longitude": 75.830957,
      "quality_flags": []
    },
    "hierarchy_path": {
      "zone": {"code": "Z-01", "name": "Jaipur Zone 1", "level": "zone"},
      "circle": {"code": "C-01", "name": "Circle 1", "level": "circle"},
      "division": {"code": "D-01", "name": "Division 1", "level": "division"},
      "subdivision": {"code": "SD-01", "name": "Subdivision 1", "level": "subdivision"},
      "substation": {"code": "SS-01", "name": "Substation 1", "level": "substation"},
      "feeder": {"code": "F-001", "name": "Feeder 1", "level": "feeder"},
      "dt": {"code": "DT-001", "name": "Malviya Nagar DT 1", "level": "dt"}
    },
    "quality_flags": []
  },
  "meta": {
    "fetched_at": "2026-10-29T13:40:00Z",
    "age_seconds": 300.0,
    "stale": false,
    "source": "store"
  }
}
```

---

### 4. Meter Consumption Time Series

Query 30-minute interval readings (kWh, kVAh, voltage) with date range filtering:

```bash
curl -s "http://localhost:8000/v1/meters/J100000/consumption?from=2026-06-20T00:00:00Z&to=2026-06-23T23:30:00Z&limit=2" | jq
```

*Sample Response:*
```json
{
  "data": [
    {
      "meter_id": "J100000",
      "timestamp": "2026-06-23T18:00:00Z",
      "raw_timestamp": "23/06/2026 23:30",
      "kwh": 48438.74,
      "kvah": 52313.84,
      "voltage_r": 226.0,
      "quality_flags": []
    }
  ],
  "meta": {
    "fetched_at": "2026-10-29T13:00:00Z",
    "age_seconds": 2700.0,
    "stale": false,
    "source": "store"
  }
}
```

---

### 5. Reconstructed Network Hierarchy Tree

Synthesizes the complete 7-level network tree with deterministic repair rules (breaking cycles, tie-breaking multiple parents, detaching dangling refs):

```bash
curl -s http://localhost:8000/v1/hierarchy | jq
```

---

### 6. Subtree Query (Recursive CTE)

Queries all descendants of any hierarchy node using SQLite recursive CTEs with depth protection:

```bash
curl -s http://localhost:8000/v1/nodes/SS-01/subtree | jq
```

---

### 7. Geospatial Nearby Search (Bounding-Box + Haversine)

Locate meters within a given radius (km) using a two-phase query (SQL bounding-box prefilter followed by exact great-circle Haversine calculation, excluding Null Island `(0,0)`):

```bash
curl -s "http://localhost:8000/v1/meters/nearby?lat=26.9389&lon=75.8309&radius_km=2.0&limit=5" | jq
```

---

### 8. Network Data Quality Report

Generates an audit report across hierarchy anomalies, meter quirks, and reading monotonicity:

```bash
curl -s http://localhost:8000/v1/quality | jq
```

*Sample Response:*
```json
{
  "data": {
    "total_meters_audited": 403,
    "total_nodes_audited": 58,
    "hierarchy_defects": {
      "multiple_parents": {},
      "dangling_parents": {},
      "orphans": [],
      "cycles": [],
      "id_mismatches": {}
    },
    "meter_defects": {
      "null_island": ["J100042"],
      "unknown_phase": [],
      "unknown_status": [],
      "missing_nameplate": [],
      "incomplete_hierarchy": []
    },
    "consumption_defects": {
      "non_monotonic_kwh": ["J100015@2026-06-22T14:30:00Z"],
      "timestamp_conversion_error": []
    },
    "quality_score": 98.7
  },
  "meta": {
    "fetched_at": "2026-10-29T13:45:00Z",
    "age_seconds": 0.0,
    "stale": false,
    "source": "store"
  }
}
```

---

### 9. RFC 7807 Error Responses

All 4xx and 5xx errors return `application/problem+json`:

```bash
curl -i -s "http://localhost:8000/v1/meters/INVALID%20ID"
```

*Response Header & Body:*
```http
HTTP/1.1 422 Unprocessable Entity
Content-Type: application/problem+json
```
```json
{
  "type": "https://urja-ops.flockenergy.tech/errors/validation-error",
  "title": "Unprocessable Entity",
  "status": 422,
  "detail": "The request parameters failed validation schema checks.",
  "instance": "/v1/meters/INVALID%20ID",
  "invalid_params": [
    {
      "name": "path -> meter_id",
      "reason": "String should match pattern '^[A-Za-z0-9_\\-\\.]+$'",
      "type": "string_pattern_mismatch"
    }
  ]
}
```

---

## Design Decisions & Trade-offs

A comprehensive, dated decision log is maintained in [DECISIONS.md](./DECISIONS.md). Key trade-offs include:

1. **Explicit `quality_flags` over Silent Coercion**:
   - *Decision*: When encountering portal anomalies (e.g. coordinates at `(0, 0)`, non-monotonic consumption readings, unknown status enums), records are ingested with descriptive tags in `quality_flags` rather than being discarded or silently mutated.
   - *Trade-off*: Consumer applications must inspect `quality_flags` if strict data cleanliness is required, but zero operational telemetry is lost.

2. **SQLite with WAL Mode & Provenance**:
   - *Decision*: Stored in embedded SQLite with write-ahead logging (WAL), foreign keys, and provenance columns (`fetched_at`, `source_endpoint`, `payload_hash`).
   - *Trade-off*: Zero external infrastructure overhead for single-node deployments. However, horizontal scaling is limited by SQLite's single-writer concurrency.

3. **In-Process Single-Flight Request Deduplication**:
   - *Decision*: Coalesces concurrent queries for stale resources via `asyncio.Future` locks so 20 simultaneous API requests trigger exactly 1 upstream request to the legacy portal.
   - *Trade-off*: Works across coroutines in a single Python process; multi-container orchestration would require a centralized coordinator (e.g., Redis).

4. **Deterministic Graph Repair**:
   - *Decision*: The graph builder resolves multiple parent definitions by selecting the lexically first parent (`sorted(parents)[0]`) and breaks cycles by converting back-edges to root nodes.
   - *Trade-off*: Reconstructs an acyclic tree deterministically without deleting any node, allowing human operators to audit flagged anomalies in `/v1/quality`.

---

## Assumptions

- **Read-Only Portal**: The portal contains no state-modifying endpoints; all mutations are strictly rejected at the HTTP client transport layer.
- **Timezone**: All portal timestamps lack timezone indicators and are assumed to represent Indian Standard Time (IST, UTC+05:30). They are stored and served as normalized UTC ISO-8601 timestamps, retaining `raw_timestamp` for provenance.
- **Hierarchy Completeness**: The 7-level breadcrumb on the meter detail page is the sole authoritative representation of infrastructure hierarchy.

---

## What Was Skipped and Why

1. **Consumer Authentication**:
   - *Why Skipped*: The assignment objective focused on wrapping, hardening, and structuring the legacy upstream portal data. Consumer authentication (e.g., OAuth2/JWT) was omitted to keep local execution frictionless for reviewers.
2. **WebSockets / Server-Sent Events (SSE)**:
   - *Why Skipped*: Real-time sync notifications were omitted in favor of pull-based audit inspection via `/v1/health` and `sync_runs` table records.
3. **Distributed Rate Limiting**:
   - *Why Skipped*: Rate limiting is enforced locally via `asyncio.Semaphore`. Distributed token-bucket coordination was avoided to eliminate external Redis dependencies.

---

## What I'd Improve with More Time

1. **Authentication & Authorization**: Add API key authentication or OAuth2 bearer tokens with role-based access control (Admin, Operator, Read-Only).
2. **Distributed Cache & Locking**: Replace in-memory SingleFlight and semaphore tracking with Redis Redlock for clustered horizontal deployments.
3. **Geospatial Materialization**: Migrate SQLite spatial queries to PostgreSQL with PostGIS (`ST_DWithin`) and materialize hierarchical paths using the PostgreSQL `ltree` extension for $O(1)$ subtree querying.
4. **CI/CD Pipeline**: Add GitHub Actions workflow for linting, security scanning (Bandit, Safety), and multi-version Python testing.
5. **Containerization**: Provide a multi-stage `Dockerfile` and `docker-compose.yml` for unified local setup.

---

## Data Scale Limitations

- **Current Operational Scale ($<50{,}000$ Nodes / $<500{,}000$ Meters)**:
  - In-memory DFS tree synthesis and SQLite recursive CTEs execute in $O(V + E)$ time and memory.
  - Queries execute in under $40\,\text{ms}$ for district-level datasets.
- **Bottlenecks at Scale ($>10\text{M}$ Records)**:
  - **Spatial Filtering**: SQLite lacks native multi-dimensional spatial indexes. Bounding-box prefilters require B-tree scans over composite latitude/longitude columns, degrading at scale.
  - **CTE Recursion Memory**: SQLite stores recursive CTE working tables in temporary disk files (`sqlite3_temp_directory`), introducing I/O latency on massive hierarchies.
  - **Python In-Memory Trees**: Constructing deeply nested Pydantic graphs with millions of nodes creates garbage collection pauses and memory pressure.
- **Production Path for Nation-Scale Grids**:
  - Migrate storage to **PostgreSQL + PostGIS** for spatial queries (`GIST` index).
  - Materialize hierarchy paths with **PostgreSQL `ltree`** or a graph database (**Neo4j**).

---

## Key Documents

- [PROTOCOL.md](./PROTOCOL.md) — Exhaustive reverse-engineered portal specification.
- [DECISIONS.md](./DECISIONS.md) — Dated register of all design trade-offs, assumptions, and mistakes.
- [REFLECTION.md](./REFLECTION.md) — Personal engineering notes and reflections.
- [openapi.json](./openapi.json) — Full OpenAPI 3.1.0 JSON schema.
