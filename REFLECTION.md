# REFLECTION.md — Engineering Notes & Decisions

> **Author Note**: These are structured draft notes reflecting on the architectural decisions, trade-offs, and discoveries made while building the `urja-api` service. Kept in draft form for rewriting into final personal narrative.

---

## 1. Architectural Framing: The "Never Trust Upstream" Philosophy

- **Scraper vs API Wrapper**: The legacy portal is a stateful SSR SvelteKit web application, not an API. Treating it as an unstable, untrusted upstream dependency shaped every architectural boundary.
- **Strict Layer Isolation**:
  - `client/` owns HTTP transport, authentication state, concurrency limits, and retry loops. It refuses non-GET requests at the transport level.
  - `adapters/` is the **only** layer that knows HTML parsing, CSS selectors, or SvelteKit devalue serialization.
  - `domain/` models represent clean, normalized domain objects with typed enums and UTC datetimes.
  - The rest of the system (`store/`, `api/`, `hierarchy/`) never inspects raw HTML or portal session cookies.
- **Outcome**: When the portal's markup or session behavior changes, only a single adapter or client method needs updates; the API contracts and database schema remain untouched.

---

## 2. Data Integrity: `quality_flags` vs. Silent Fixing

- **The Utility Data Dilemma**: Utility datasets are notoriously dirty — null island coordinates `(0.0, 0.0)`, non-monotonic cumulative kWh readings, missing nameplate fields, and incomplete breadcrumb hierarchies.
- **The Decision**: Rather than silently coercing or discarding dirty rows, every model includes a `quality_flags: list[str]` array.
  - An anomalous coordinate is stored as `(0.0, 0.0)` with flag `null_island_coordinates`.
  - Non-monotonic consumption readings are preserved with flag `non_monotonic_kwh`.
  - Unknown phase enums (like `"4"`) are preserved with flag `unknown_phase:4`.
- **Takeaway**: Downstream analytics and billing applications can decide whether to filter, interpolate, or alert on bad telemetry, without the API silently corrupting the historical audit trail.

---

## 3. Persistence, Provenance & Idempotency

- **Why Embedded SQLite Was Right-Sized**: For single-node deployment wrapping a 403-meter utility district, SQLite with Write-Ahead Logging (WAL) and foreign keys enabled instantaneous testing, zero ops overhead, and single-file backups.
- **Exact Index Justifications**:
  - `idx_meters_dt_code`: Supports hierarchy tree queries by DT parent code.
  - `idx_meters_serial_no`: Supports fast nameplate search by meter serial number.
  - `idx_readings_timestamp`: Optimizes cross-meter consumption time-window aggregation.
  - `idx_hierarchy_nodes_parent_code`: Accelerates parent-child lookups and recursive CTE queries.
  - `idx_sync_runs_started_at`: Fast retrieval of the most recent sync run for `/health` freshness checks.
- **Provenance by Default**: Storing `fetched_at`, `source_endpoint`, and SHA-256 `payload_hash` on every entity made change detection trivial: re-syncing unchanged portal pages yields `items_unchanged` without unnecessary database updates.

---

## 4. Freshness Architecture & Request Coalescing

- **Tiered Freshness Policies**:
  - Master metadata (`meters`, `transformers`): 1-hour TTL.
  - Recent consumption (<24h old): 5-minute TTL (frequently updated telemetry).
  - Historical consumption (>24h old): 30-day TTL (treated as immutable meter log).
- **Single-Flight Coalescing**:
  - If 20 concurrent requests hit `/v1/meters/J100000` when the cache has expired, `SingleFlight` uses an `asyncio.Future` lock to ensure only **one** coroutine executes the upstream portal fetch. The other 19 await the identical result.
  - Protects the fragile upstream portal from being thrashed by thundering herds.
- **Stale-While-Revalidate**: If upstream refresh fails during background revalidation, existing store records are served with `meta.stale = true` rather than failing the user request.

---

## 5. Network Graph Repair & Recursive CTEs

- **Topological Invariant: Zero Deletions**:
  - When reconstructing the 7-level hierarchy (`Zone › Circle › Division › Subdivision › Substation › Feeder › DT`), dirty data showed cycles, dangling parents, and duplicate parent links.
  - Decision: **Never delete a node.** Every node is preserved in the graph.
- **Deterministic Repair Rules**:
  - *Multiple parents*: Sort parents lexically and select `parents[0]`; record `multiple_parents:[...]` defect flag.
  - *Cycles*: Trace paths via DFS; break cycle at the back-edge by setting `parent = None`; record `cycle_detected:...` flag.
  - *Dangling references*: If parent code is missing from the dataset, detach to root; record `dangling_parent:...` flag.
  - *Orphans*: Flag non-Zone nodes with no parent.
- **Subtree Queries via SQLite Recursive CTE**:
  - Implemented `get_node_subtree_cte()` using SQL `WITH RECURSIVE`.
  - Added an explicit depth guard (`WHERE s.depth < 20`) to guarantee that circular references in database records can never produce infinite recursion or thread hangs.

---

## 6. Geospatial Queries & Scale Horizons

- **Two-Phase Spatial Filtering**:
  1. *Phase 1*: Fast bounding-box prefilter in SQL (`latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?`), excluding Null Island `(0.0, 0.0)`.
  2. *Phase 2*: Exact Haversine great-circle calculation in Python, filtering by `distance <= radius_km` and sorting ascending.
- **Scale Limits of the Current Approach**:
  - Works in $<40\,\text{ms}$ for district grids ($<50{,}000$ nodes).
  - Struggles at $>10\text{M}$ meters due to lack of native multi-dimensional R-tree indexing in standard SQLite, memory limits on recursive CTE breadth, and Python memory overhead for in-memory graph trees.
  - Production recommendation: PostgreSQL with **PostGIS** (`ST_DWithin`) and materialized path indexing (**`ltree` extension**) or **Neo4j**.

---

## 7. Surprises, Traps & Discoveries During Development

1. **The AnyIO Worker Thread Event Loop Trap**:
   - In Python 3.9, synchronous dependency functions in FastAPI (`def get_client()`, `def get_freshness()`) are run in AnyIO worker threads.
   - Calling `asyncio.Lock()` or `asyncio.Semaphore()` inside those threads raised `RuntimeError: There is no current event loop in thread 'AnyIO worker thread'`.
   - *Fix*: Made dependency providers `async def` and implemented lazy property initialization on `PortalClient.semaphore`, `PortalClient.auth_lock`, and `SingleFlight.lock`.
2. **Duplicate Exception Class Masking**:
   - `MeterNotFoundError` was independently defined in `src/urja/client/exceptions.py` (inheriting from `PortalError`) and in `src/urja/api/errors.py` (inheriting from `Exception`).
   - When the client raised `MeterNotFoundError`, the API exception handler didn't recognize it as the API's class and fell through to the `PortalError` handler, rendering a 502 instead of 404!
   - *Fix*: Consolidated into a single canonical class in `exceptions.py` and re-exported it.
3. **SvelteKit "Soft" Session Expirations**:
   - The portal doesn't return HTTP 401 when sessions expire; it redirects HTTP 303 to `/` and renders the HTML login form with HTTP 200.
   - *Fix*: Added detection for `name="password"` input fields in responses to unmask expired sessions and trigger automatic re-login.

---

## 8. What I Would Do Differently Next Time

- **Containerization from Day One**: Add a Dockerfile and docker-compose stack early to standardize Python versions and eliminate OS-specific quirks.
- **Redis for Distributed State**: While in-memory `SingleFlight` and local `asyncio.Semaphore` are clean for a single process, a real production utility gateway needs distributed rate limiting and distributed caching.
- **Formal OpenAPI Contract First**: Generating OpenAPI from FastAPI code worked smoothly, but drafting the schema first would have clarified envelope design earlier.
