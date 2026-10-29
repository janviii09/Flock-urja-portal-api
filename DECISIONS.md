# DECISIONS.md — Trade-offs, Assumptions, and Mistakes

One line per decision, dated. Categories: `DECISION`, `ASSUMPTION`, `TRADE-OFF`, `MISTAKE`.

---

| Date       | Type        | Decision |
|------------|-------------|----------|
| 2026-10-27 | DECISION    | Python 3.9+ + FastAPI + httpx + Pydantic v2 + SQLite — mature, fast, single-binary-deployable stack. |
| 2026-10-27 | DECISION    | All portal-facing code isolated in `client/` (transport) and `adapters/` (parsing). Rest of codebase never sees raw portal data. |
| 2026-10-27 | DECISION    | `quality_flags: list[str]` on every domain model instead of silent data cleaning. Consumers decide what to trust. |
| 2026-10-27 | DECISION    | Credentials via env vars only (`pydantic-settings`), `.env` file git-ignored. |
| 2026-10-27 | ASSUMPTION  | Export button on `/transformers` produces a CSV with all meters. Format unconfirmed (requires authenticated download). |
| 2026-10-27 | ASSUMPTION  | Search on `/meters` is server-side filtering via query param, not client-side JS filter. |
| 2026-10-27 | ASSUMPTION  | Readings on meter detail page are the latest ~15 entries; no pagination or date range selector observed. |
| 2026-10-27 | TRADE-OFF   | Using SvelteKit `__data.json` endpoints for data extraction instead of HTML parsing — structured but tightly coupled to SvelteKit internals. Will implement HTML parsing as fallback. |
| 2026-10-27 | DECISION    | Hierarchy is 7 levels (Zone→Circle→Division→Subdivision→Substation→Feeder→DT). Breadcrumb on meter detail page is the only source for the full path. |
| 2026-10-27 | DECISION    | Monorepo single-service layout (`src/urja/`) — no microservices, right-sized for the problem. |
| 2026-10-28 | DECISION    | Portal client enforces read-only guard (non-GET methods rejected at transport boundary). |
| 2026-10-28 | DECISION    | Session expiry detects both HTTP 401/403 and SvelteKit redirects to login page with HTML password form. |
| 2026-10-28 | DECISION    | Re-authentication on session expiry retries exactly once to prevent infinite auth loops. |
| 2026-10-28 | DECISION    | Concurrency limited at HTTP client level via `asyncio.Semaphore` to protect legacy portal. |
| 2026-10-29 | DECISION    | HTML adapters implemented via BeautifulSoup4 + lxml; raise `PortalShapeError(field=...)` on structural deviation. |
| 2026-10-29 | DECISION    | Portal timestamps treated as IST (UTC+05:30) and converted to UTC datetime, keeping `raw_timestamp` for provenance. |
| 2026-10-29 | DECISION    | Data quirks (null island `(0,0)`, non-monotonic kWh, unknown enums, missing nameplates) captured in `quality_flags` without dropping rows. |
| 2026-10-29 | DECISION    | SQLite persistence with WAL mode, foreign keys, and per-entity provenance (`fetched_at`, `source_endpoint`, `payload_hash`). |
| 2026-10-29 | DECISION    | Exact index justifications: `meters(dt_code)` for hierarchy, `meters(serial_no)` for search, `readings(timestamp)` for cross-meter ranges, `hierarchy_nodes(parent_code, level)` for tree traversal, `sync_runs(started_at DESC)` for freshness. |
| 2026-10-29 | DECISION    | Entity-level change detection on upsert: only actual field updates count as `CHANGED`; identical data is marked `UNCHANGED`. |
| 2026-10-29 | DECISION    | Fault-isolated sync transactions: individual page or meter detail failures roll back atomically without corrupting previously synced records. |
| 2026-10-29 | DECISION    | Tiered freshness TTLs: master data 1h, recent consumption (<24h) 5m, historical consumption (>24h) 30d (immutable). |
| 2026-10-29 | DECISION    | Single-flight deduplication via `asyncio.Future` coalesces concurrent queries for expired resources into a single upstream request. |
| 2026-10-29 | DECISION    | RFC 7807 Problem Details (`application/problem+json`) enforced for all 4xx/5xx error responses. |
| 2026-10-29 | DECISION    | Cursor pagination (`meter_id > cursor`) on `/v1/meters` for stable O(1) page access. |
| 2026-10-29 | DECISION    | Deterministic hierarchy repair: normalize IDs via `UPPER(TRIM())`; resolve multiple parents to lexically first parent; break cycles by detaching back-edge to root (`parent = None`); detach dangling parents to root; flag non-Zone root nodes as orphans. |
| 2026-10-29 | DECISION    | Invariant: Network graph repair never deletes or drops nodes; 100% of discovered nodes are preserved in the tree and defect report. |
| 2026-10-29 | DECISION    | Subtree querying via SQLite recursive CTE (`WITH RECURSIVE`) with a depth guard (`depth < 20`) to eliminate infinite recursion risk on graph anomalies. |
| 2026-10-29 | DECISION    | Two-phase spatial query for `/v1/meters/nearby`: bounding-box prefilter in SQL followed by exact Haversine great-circle calculation; explicitly excludes Null Island `(0,0)`. |
| 2026-10-29 | TRADE-OFF   | In-memory DFS tree synthesis and SQLite recursive CTEs are optimal for utility district scale (<50k nodes), but struggle at nation scale (>10M nodes) where PostgreSQL+PostGIS with materialized path indexing (ltree) or Neo4j is required. |
| 2026-10-29 | DECISION    | Upstream error containment: registered base `PortalError` and `httpx.HTTPError` handlers to map all portal transport and shape failures to RFC 7807 502 Bad Gateway instead of leaking 500s. |
| 2026-10-29 | DECISION    | Log scrubbing: implemented `SecretRedactingFilter` on root log handler to strip passwords, bearer/basic tokens, cookies, and `URJA_PASSWORD` from formatted logs and record arguments. |
| 2026-10-29 | DECISION    | Async lock lazy initialization: lazy properties on `PortalClient` and `SingleFlight` bind semaphores/locks to the active event loop, preventing AnyIO thread worker `RuntimeError`. |
| 2026-10-29 | DECISION    | Strict path & query validation: enforced regex patterns on `meter_id` and `node_id` path parameters; rejected inverted consumption date windows (`from > to`) with 400. |
| 2026-10-29 | MISTAKE     | Assumed portal required HTML scraping for meters and hierarchy. Actual inspection of portal frontend bundle revealed verified JSON endpoints `/portal/export`, `/portal/meters/search`, and `/portal/meters/{id}/energy`. |
| 2026-10-29 | DECISION    | Switched primary ingestion to verified JSON endpoints: bulk export via `/portal/export` and telemetry via `/portal/meters/{id}/energy`. Retained HTML adapters as fallback. |
| 2026-10-29 | DECISION    | Dynamic ephemeral session signing: fetch signing secret on-demand from `GET /portal/keys` for `/portal/export`; never persist or hardcode secrets in config or code. |
| 2026-10-29 | DECISION    | Full-path hierarchy keying: keyed hierarchy nodes by canonical path (e.g. `Z-01/C-01/D-01/SD-01`) instead of bare codes to eliminate collision when codes (e.g. `SD-01`) repeat under different parents. |
| 2026-10-29 | DECISION    | Preserved dual timestamps for telemetry: stored literal `raw_timestamp` alongside normalized UTC `timestamp`; composite primary key `(meter_id, raw_timestamp)` ensures idempotent deduplication. |
| 2026-10-29 | DECISION    | SvelteKit CSRF compatibility: added `Origin` and `Referer` headers to `POST /login` to avoid SvelteKit 403 cross-site form submission rejection. |
| 2026-10-29 | DECISION    | Session expiry discernment: excluded `signature_invalid` responses from session expiry triggers to prevent false re-auth thrashing. |
| 2026-10-29 | DECISION    | Bounded telemetry concurrency: limited concurrent energy requests to 4 parallel workers via `asyncio.Semaphore` to respect legacy server rate limits. |
| 2026-10-30 | DECISION    | Completely retired and removed obsolete HTML scraping adapters (`base.py`, `meters_list.py`, `meter_detail.py`, `transformers.py`) and exploratory probe scripts. Standardized 100% of network synchronization on verified JSON endpoints (`/portal/export`, `/portal/keys`, `/portal/meters/search`, `/portal/meters/{id}/energy`, `/portal/dts`). |
