# REFLECTION.md — Engineering Reflection

> Written for the **Flock Energy — Engineering Take-Home** assignment.
> This document answers the five reflection questions specified in the evaluation brief.

---

## 1. What assumptions did you make?

- **Read-Only Portal**: Assumed the legacy portal has no state-modifying actions needed and treated it as strictly read-only. We enforced this at the transport layer with a `ReadOnlyGuard` that rejects non-`GET` HTTP requests.
- **Timezone**: The portal displays naive datetime strings (`DD/MM/YYYY HH:MM`) without timezone offsets. Because Urja is an Indian electricity distribution utility context, we assumed Indian Standard Time (IST, UTC+05:30). All timestamps are parsed, converted, and stored as normalized UTC ISO-8601 strings, while retaining the `raw_timestamp` string for immutable audit provenance.
- **Cumulative Registers vs. Deltas**: Assumed consumption readings in the portal table represent cumulative meter registers (cumulative kWh and kVAh readings every 30 minutes) rather than interval usage deltas, requiring monotonicity and rollover checks.
- **Static Snapshot of Historical Readings**: The latest readings on the portal ended on 30/06/2026. We verified via repeated fetches that historical readings do not mutate, justifying a tiered caching strategy: 1 hour for master metadata, 5 minutes for recent readings (<24h old), and 30 days (immutable) for historical readings.
- **Hierarchy Completeness**: Assumed the 7-level breadcrumb on the meter detail page (`Zone › Circle › Division › Subdivision › Substation › Feeder › DT`) is the sole authoritative representation of the distribution network topology, as no independent hierarchy endpoint exists on the legacy portal.

---

## 2. Which part was the most difficult, and how did you get unstuck?

- **The Difficulty**: Handling the legacy portal's "soft" session expiration and re-login mechanics under concurrent traffic. The portal does not return standard HTTP 401 Unauthorized status codes when a session dies; instead, it issues an HTTP 303 redirect back to `/` and renders the HTML login form with an HTTP 200 status code. Furthermore, when multiple concurrent requests (such as during a multi-page crawl or burst API traffic) encountered an expired session simultaneously, naive retry logic caused thundering-herd re-logins that risked IP rate-limiting or account lockouts.
- **How I Got Unstuck**:
  1. *Shape-based expiry detection*: Rather than relying on HTTP status codes alone, the adapter inspects response bodies for `name="password"` and `name="email"` input fields on protected data endpoints to reliably detect unauthenticated redirects.
  2. *Single-flight re-authentication*: Implemented an asynchronous lock (`_auth_lock`) in `PortalClient`. When $N$ concurrent coroutines encounter an expired session, only the first caller initiates the `POST /login` call. The remaining $N-1$ await the existing login in-flight, update their session cookies, and retry their original request once.

---

## 3. If you had another day, what would you improve?

- **API Consumer Authentication & Authorization**: The service currently exposes open endpoints for local evaluation simplicity. In a production setting, I would add OAuth2 JWT bearer token or API key authentication with role-based access control (Operator, Analyst, Read-Only).
- **Distributed Coordination & Caching**: The current `SingleFlight` request deduplication and concurrency semaphores run in-process in Python memory. To scale horizontally across multiple container replicas behind a load balancer, I would introduce a Redis backend for distributed rate-limiting and cache locking (e.g. Redlock).
- **Continuous Autonomous Background Sync**: Currently, sync runs on-demand or during cache freshness evaluations. I would implement an autonomous background worker daemon (using Celery or APScheduler) that periodically polls the portal, detects changes via payload hashes, and keeps the local SQLite store continuously synchronized without needing incoming API traffic.

---

## 4. What mistake did you make while solving this (there's always one)?

- **The Legacy Portal Endpoint Assumption**:
  Initially, I assumed the portal only exposed HTML tables for meters and hierarchy, and started parsing HTML with BeautifulSoup4. When doing a deep inspection of the portal's client-side SvelteKit bundle (`nodes/4.2Bgc2kUI.js`), I discovered verified JSON endpoints: `/portal/export` (bulk unpaginated dump of all 403 meters), `/portal/meters/search`, and `/portal/meters/{id}/energy`. Furthermore, `/portal/export` required an ephemeral HMAC-SHA256 signature calculated from a session secret fetched dynamically from `GET /portal/keys`.
  *How it was resolved*: Replaced the HTML scraping pipeline with native JSON adapters (`export.py`, `energy.py`, `search.py`). In `PortalClient`, implemented ephemeral HMAC-SHA256 signing via `/portal/keys`, added `Origin` and `Referer` headers to `POST /login` to satisfy SvelteKit CSRF checks, and keyed hierarchy nodes by canonical full path to handle multi-parent codes (`SD-01`) cleanly.
- **The Python 3.9 AnyIO Worker Thread Event Loop Trap**:
  In FastAPI, I initially defined the dependency injection providers (`get_client()`, `get_freshness()`) as standard synchronous functions (`def get_client()`). FastAPI automatically offloads synchronous dependency functions to AnyIO worker threads via `anyio.to_thread.run_sync`. When `PortalClient` and `SingleFlight` instantiated `asyncio.Lock()` and `asyncio.Semaphore()`, Python 3.9 threw:
  `RuntimeError: There is no current event loop in thread 'AnyIO worker thread'`.
  *How it was resolved*: Converted all dependency providers to `async def` so FastAPI executes them directly on the running async event loop, and refactored `self.semaphore`, `self.auth_lock`, and `SingleFlight.lock` to lazily instantiate on their first asynchronous access.
- **Duplicate Exception Class Masking**:
  Early on, `MeterNotFoundError` was defined in both `src/urja/client/exceptions.py` (subclassing `PortalError`) and `src/urja/api/errors.py` (subclassing `Exception`). When an unknown meter triggered a 404 from the portal, FastAPI's exception handler did not recognize the client's exception as the API's class and fell through to the general `PortalError` handler, incorrectly returning a 502 Bad Gateway instead of a 404 Not Found. Consolidating into a single canonical class resolved the masking.

---

## 5. If you were reviewing your own submission, what would you criticise?

- **Geospatial Scalability Boundary**: The two-phase spatial query (SQL bounding-box prefilter + Python Haversine calculation) is straightforward and executes in $<10\,\text{ms}$ for the 403 meters in the dataset. However, standard SQLite lacks native multi-dimensional spatial R-tree indexing. At $>10\text{M}$ meters, this approach would degrade into expensive B-tree index scans. For a production utility grid, I would criticize not migrating to PostgreSQL with PostGIS (`ST_DWithin` with a GiST spatial index).
- **In-Memory Graph Synthesis**: The `HierarchyGraph` builds the entire 7-level tree in Python heap memory to perform cycle breaking and parent tie-breaking. While instantaneous for thousands of district nodes, this approach creates memory overhead at nation scale (>1M nodes). A mature architecture would materialize hierarchical paths in the database (e.g. PostgreSQL `ltree` extension) to evaluate subtrees in $O(1)$ without recursion.
- **No Inbound Rate Limiting**: While the service carefully protects the *upstream* legacy portal with concurrency semaphores and exponential backoff, it does not enforce rate limits on incoming API consumers hitting `/v1/meters` or `/v1/hierarchy`. A runaway client could overwhelm the local API server.
