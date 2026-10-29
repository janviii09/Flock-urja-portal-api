# PROTOCOL.md — How the Urja Meter Ops Portal Actually Works

> Portal facts only. Every statement here comes from direct observation of
> the live portal at `https://urja-ops.flockenergy.tech`.
> Nothing is invented; gaps are marked **UNKNOWN**.

---

## 1. Technology

- **Framework**: SvelteKit (SSR + client-side hydration)
- **Favicon**: Svelte logo (SVG embedded in HTML)
- **Page title pattern**: `"Urja Meter Ops — <Page Name>"`

## 2. Authentication

| Detail        | Observed Value                                |
|---------------|-----------------------------------------------|
| Login URL     | `GET /` or `GET /login` (redirects to root)   |
| Method        | `<form method="POST">` (SvelteKit form action) |
| Fields        | `name="email"` (type email), `name="password"` (type password) |
| CSRF token    | **None observed** — no hidden fields in form  |
| Session       | Cookie-based (SvelteKit default)              |
| Unauthenticated | Redirects to login page                     |
| Sign out      | "Sign out" button in top nav bar              |

## 3. Navigation

Top nav bar: **Urja Meter Ops** | Meters | Transformers | … | Ops Desk | Sign out

## 4. Meters List — `/meters`

| Detail           | Observed                                           |
|------------------|-----------------------------------------------------|
| Total            | 403 meters (shown as "403 total" header)            |
| Rows per page    | 20                                                  |
| Pages            | 21                                                  |
| Pagination       | `?page=N` (Previous / "Page X of Y" / Next buttons) |
| Search           | Text input: "Search by meter number or serial…"     |
| Table columns    | METER, SERIAL, MAKE, PHASE, STATUS, DT              |
| Meter link       | `<a href="/meters/{meterId}">`                      |

### Observed data values

- **METER IDs**: `J100000` – `J100402` (sequential, J-prefixed, 6-digit zero-padded)
- **SERIAL**: Mixed prefixes: `SE`, `GE`, `AL`, `HP`, `L&` — 5-digit numbers
- **MAKE**: HPL, L&T, Genus, Allied, Secure
- **PHASE**: `single`, `three`
- **STATUS**: Installed, Faulty, Decommissioned
- **DT**: `DT-001` through `DT-0XX`

## 5. Meter Detail — `/meters/{meterId}`

### 5a. Hierarchy Breadcrumb

Displayed at top, below "← Back to meters" link:
```
Jaipur Zone 1 (Z-01) › Circle 1 (C-01) › Division 1 (D-01) ›
Subdivision 1 (SD-01) › Substation 1 (SS-01) › Feeder 1 (F-001) ›
Malviya Nagar DT 1 (DT-001)
```

**Pattern**: `{Name} ({Code})` separated by ` › `, 7 levels:
1. Zone (Z-XX)
2. Circle (C-XX)
3. Division (D-XX)
4. Subdivision (SD-XX)
5. Substation (SS-XX)
6. Feeder (F-XXX)
7. DT (DT-XXX)

### 5b. Nameplate Section

| Field               | Example          |
|---------------------|------------------|
| Meter ID            | J100000          |
| Serial No           | SE33962          |
| Make                | HPL              |
| Phase Type          | single           |
| Installation Status | Decommissioned   |
| Installation Type   | Whole Current    |

### 5c. Location Section

Format: `{latitude}, {longitude}` as high-precision floats.
Label: "Latitude / Longitude"

Example: `26.938961002479868, 75.83095696146852`

### 5d. Consumption Section

Header shows `Latest: DD/MM/YYYY HH:MM`

| Column    | Type       | Example     |
|-----------|------------|-------------|
| TIMESTAMP | DD/MM/YYYY HH:MM | 23/06/2026 23:30 |
| KWH       | float      | 48438.74    |
| KVAH      | float      | 52313.84    |
| V(R)      | int/float  | 226         |

- Readings appear to be at **30-minute intervals**.
- About **15 readings** shown per meter detail page.
- **UNKNOWN**: Whether readings are paginated or if only the latest N are shown.
- KWH/KVAH values are **cumulative** (monotonically increasing).

## 6. Transformers — `/transformers`

| Detail          | Observed                              |
|-----------------|----------------------------------------|
| Page title      | "Distribution Transformers"            |
| Table columns   | CODE, NAME, FEEDER, CAPACITY (KVA)     |
| Pagination      | **None** — all DTs on one page         |
| Detail page     | **Does not exist** (`/transformers/DT-001` → 404) |
| Export button   | "Export all meters" — top-right corner |

### Observed values

- **CODE**: `DT-001` through `DT-0XX`
- **NAME**: Area-based names like "Malviya Nagar DT 1", "Mansarovar DT 2"
- **FEEDER**: `F-001` through `F-0XX` (1:1 with DT codes observed so far)
- **CAPACITY (KVA)**: 63, 100, 160, 250, 400

## 7. Export — "Export all meters"

- Button on `/transformers` page.
- **ASSUMPTION**: Triggers a CSV download containing all meters with details.
- Exact endpoint and format **need confirmation** (requires auth, download in browser).

## 8. Observed Portal API & Data Endpoints

In addition to HTML SSR, the following direct JSON and SvelteKit endpoints exist:

| Endpoint | Method | Response Shape / Details |
|---|---|---|
| `/portal/meters/{id}/geo` | GET | `{data: {latitude: "...", longitude: "..."}}` (strings with ~15 decimal digits) |
| `/portal/meters/{id}/energy` | GET | `{data: [{timestamp, kwh, kvah, voltR, ...}]}`. 30-min steps, 7-day window (e.g. ending 30/06/2026 23:30), `DD/MM/YYYY HH:mm` no tz |
| `/meters/{id}/__data.json?x-sveltekit-invalidated=001` | GET | SvelteKit devalue format: nameplate detail + 7-level breadcrumb (`Name (CODE)`) |
| `/meters/__data.json` | GET | Dehydrated SvelteKit meters list |
| `/transformers/__data.json` | GET | Dehydrated SvelteKit transformers list |

- **Hierarchy Discovery**: There is **no dedicated hierarchy endpoint**. The full 7-level tree must be synthesized by aggregating breadcrumbs across all 403 meters (`/meters/{id}`).
- **Login Action Response**: SvelteKit form action returns `{"type": "redirect", "status": 303, "location": "/meters"}` on successful auth.

## 9. Quirks & Edge Cases

- **No CSRF token** on login form.
- **Login Redirect**: Returns 303 redirect to `/meters`.
- **No transformer detail pages** — the CODE values are not clickable/linked.
- **Coordinate precision** varies — up to 15+ decimal places; strings in JSON geo endpoint.
- **L&T make** contains an ampersand (`&`) which appears as `L&amp;T` in HTML.
- **Decommissioned meters** still show readings data.
- **Timestamp format**: Portal timestamps are unzoned local time (`DD/MM/YYYY HH:mm`, assumed Asia/Kolkata IST UTC+05:30).
- **Search** is by "meter number or serial" — **UNKNOWN** if server-side or client filter.

