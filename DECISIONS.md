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


