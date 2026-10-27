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
