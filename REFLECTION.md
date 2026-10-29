# Reflection

Notes on the approach, assumptions, trade-offs, and mistakes while building the Urja Meter Ops API.

---

## 1. What assumptions did you make?

A few core assumptions shaped how the system was built:

- **The portal is strictly read-only**: There are no write or update actions exposed by the portal. I treated it as an immutable source of truth and enforced this at the HTTP transport layer with a guard that blocks non-GET methods before they can touch the network.
- **Timestamps are in Indian Standard Time (IST)**: The portal displays unzoned datetime strings like `23/06/2026 23:30`. Since Urja is an Indian power distribution context, I assumed IST (UTC+05:30). The service parses these into normalized UTC ISO-8601 strings for storage and querying, while keeping the original string intact in `raw_timestamp` for auditability.
- **Consumption readings are cumulative registers**: The meter reading table shows cumulative energy totals (kWh and kVAh counter readings every 30 minutes) rather than interval usage deltas. The quality audit checks for monotonicity on these cumulative totals to catch counter resets or meter swaps.
- **Historical readings are static**: The dataset on the portal ends on 30/06/2026. Repeated fetches confirmed that historical readings do not mutate over time, which allowed for aggressive caching: master metadata expires in 1 hour, recent readings (<24h) in 5 minutes, and historical readings are effectively immutable (30-day TTL).
- **The 7-level breadcrumb is the only source of network hierarchy**: There is no dedicated hierarchy endpoint on the portal. The only place the full distribution tree (`Zone › Circle › Division › Subdivision › Substation › Feeder › DT`) appears is in the breadcrumbs on individual meter pages and the bulk export.

---

## 2. Which part was the most difficult, and how did you get unstuck?

The trickiest problem was handling the portal's "soft" session expiration under concurrent requests.

When a session expires, the portal does not return a clean HTTP 401 or 403. Instead, it issues an HTTP 303 redirect back to `/` and serves the HTML login page with a 200 OK status. If you have several concurrent coroutines fetching data — say during a sync run or a burst of API calls — every single one would see the redirect at the same time. A naive retry loop resulted in a thundering herd where every worker tried to log in independently, risking IP rate-limiting or account lockout.

Two things got this unstuck:

1. **Detecting soft expiration by response shape**: Rather than relying on HTTP status codes alone, the client inspects response bodies on data endpoints for login form markers (`name="password"` and `name="email"`). If a data endpoint returns a login form, the client treats it as an expired session regardless of the 200 status.
2. **Single-flight re-authentication**: I added an `asyncio.Lock` around login in the `PortalClient`. When N concurrent requests discover an expired session, only the first one actually sends a `POST /login`. The remaining N - 1 coroutines wait on the same in-flight login, pick up the refreshed session cookies once it finishes, and retry their original request once.

---

## 3. If you had another day, what would you improve?

If I had more time, I would focus on three areas:

- **API consumer auth**: Right now the API endpoints are completely open so anyone reviewing or running locally can hit them immediately without having to set up API keys or tokens. In any real deployment, I'd add API key or JWT bearer authentication with basic role-based access (admin, operator, read-only).
- **Distributed coordination**: The single-flight request coalescing and the concurrency semaphore both live in Python process memory. That works well for a single instance, but if you ran multiple containers behind a load balancer, each replica would independently hit the portal. I'd move the locking and deduplication to Redis (using something like Redlock) to coordinate across replicas.
- **Continuous background sync daemon**: Sync is currently triggered either explicitly (`python -m urja.sync`) or on-demand when fresh data is needed. A scheduled background worker (Celery, APScheduler, or a lightweight async loop) that periodically polls the portal, tracks payload hashes, and writes changes would keep the local store warm without adding latency to user requests.

---

## 4. What mistake did you make while solving this?

A couple of notable ones came up during development:

- **Assuming the portal only served HTML**:
  Early on, I saw the portal's web interface and started writing BeautifulSoup scrapers for the HTML tables. While digging into the client-side SvelteKit bundle (`nodes/4.2Bgc2kUI.js`), I noticed the portal actually has internal JSON endpoints: `/portal/export` (a complete dump of all 403 meters), `/portal/meters/search`, and `/portal/meters/{id}/energy`. What looked like a purely HTML-scraping project actually had a cleaner JSON interface hidden behind an ephemeral HMAC-SHA256 signature requirement (`GET /portal/keys`). Tossing out the HTML parsers and rewriting around native JSON adapters took extra time, but it made ingestion significantly faster and more reliable.

- **FastAPI sync dependency trap on Python 3.9**:
  I initially wrote dependency providers like `get_client()` and `get_freshness()` as plain synchronous functions (`def get_client()`). FastAPI runs sync dependencies in worker threads using `anyio.to_thread.run_sync`. Because `PortalClient` and `SingleFlight` create `asyncio.Lock` and `asyncio.Semaphore` objects in their constructors, Python 3.9 failed immediately with `RuntimeError: There is no current event loop in thread 'AnyIO worker thread'`. The fix was converting the dependency functions to `async def` so FastAPI runs them on the main event loop, and lazily initializing locks on their first async call.

- **Duplicate exception classes masking 404s**:
  I had defined `MeterNotFoundError` in two places: `src/urja/client/exceptions.py` (inheriting from `PortalError`) and `src/urja/api/errors.py` (inheriting from `Exception`). When an unknown meter triggered a 404, FastAPI's error handler didn't match the client's class to the API's class. The exception fell through to the generic `PortalError` handler and returned a 502 Bad Gateway instead of a 404 Not Found. Consolidating into a single canonical exception class fixed it.

---

## 5. If you were reviewing your own submission, what would you criticise?

- **Geospatial querying doesn't scale to millions**: The two-phase bounding-box prefilter in SQL followed by a Python Haversine formula is simple, has zero external dependencies, and executes in under 10 ms for the 403 meters here. But standard SQLite lacks multi-dimensional spatial indexing. At scale (say, 500,000+ meters), the bounding box check becomes an expensive composite index scan. For production utility workloads, this should be PostgreSQL + PostGIS with an R-tree / GiST index (`ST_DWithin`).
- **In-memory hierarchy tree reconstruction**: The `HierarchyGraph` loads all nodes into Python memory to detect multiple parents, break cycles, and assemble the nested JSON tree. That's instantaneous for 58 nodes across Jaipur, but it would become a memory hog and GC bottleneck for a state or national grid with hundreds of thousands of substations and transformers. A production system would store materialized paths in the database (like PostgreSQL's `ltree` extension) so subtree queries become fast prefix scans without recursive in-memory processing.
- **No client-side rate limiting on the API**: The service takes great care to protect the *upstream* portal with concurrency semaphores and exponential backoff, but it doesn't protect *itself* from consumer abuse. An incoming burst of requests to `/v1/hierarchy` or `/v1/quality` could tie up the server. Adding token-bucket rate limiting (e.g. via `slowapi`) would be necessary before exposing this to untrusted clients.
