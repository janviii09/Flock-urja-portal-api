"""Recorded portal HTML fixtures for deterministic adapter tests.

Each fixture is a raw string exactly as the portal would return it.
These are hand-crafted from observed portal pages, not copied verbatim,
so they contain no actual user data, but faithfully mirror the structure.
"""

# ── Meter list page (partial, page 1) ────────────────────────────────
# Observed: 20 rows per page, 21 pages, 403 total meters.
# Table columns: METER, SERIAL, MAKE, PHASE, STATUS, DT
# Pagination: Previous / Page X of Y / Next

METERS_LIST_PAGE_HTML = """\
<table>
  <thead>
    <tr>
      <th>METER</th><th>SERIAL</th><th>MAKE</th>
      <th>PHASE</th><th>STATUS</th><th>DT</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><a href="/meters/J100000">J100000</a></td>
      <td>SE33962</td><td>HPL</td><td>single</td>
      <td>Decommissioned</td><td>DT-001</td>
    </tr>
    <tr>
      <td><a href="/meters/J100001">J100001</a></td>
      <td>GE84132</td><td>L&amp;T</td><td>single</td>
      <td>Installed</td><td>DT-002</td>
    </tr>
    <tr>
      <td><a href="/meters/J100002">J100002</a></td>
      <td>AL28136</td><td>L&amp;T</td><td>single</td>
      <td>Installed</td><td>DT-003</td>
    </tr>
  </tbody>
</table>
<div class="pagination">
  <button disabled>Previous</button>
  <span>Page 1 of 21</span>
  <button>Next</button>
</div>
"""

# ── Meter detail page ────────────────────────────────────────────────
# Observed sections: breadcrumb hierarchy, Nameplate, Location, Consumption.
# Hierarchy breadcrumb: "Zone (code) › Circle (code) › ... › DT (code)"

METER_DETAIL_HTML = """\
<div class="breadcrumb">
  Jaipur Zone 1 (Z-01) › Circle 1 (C-01) › Division 1 (D-01) ›
  Subdivision 1 (SD-01) › Substation 1 (SS-01) › Feeder 1 (F-001) ›
  Malviya Nagar DT 1 (DT-001)
</div>

<section class="nameplate">
  <h2>Nameplate</h2>
  <dl>
    <dt>Meter ID</dt><dd>J100000</dd>
    <dt>Serial No</dt><dd>SE33962</dd>
    <dt>Make</dt><dd>HPL</dd>
    <dt>Phase Type</dt><dd>single</dd>
    <dt>Installation Status</dt><dd>Decommissioned</dd>
    <dt>Installation Type</dt><dd>Whole Current</dd>
  </dl>
</section>

<section class="location">
  <h2>Location</h2>
  <p class="coords">26.938961002479868, 75.83095696146852</p>
  <p class="label">Latitude / Longitude</p>
</section>

<section class="consumption">
  <h2>Consumption</h2>
  <span class="latest">Latest: 30/06/2026 23:30</span>
  <table>
    <thead>
      <tr><th>TIMESTAMP</th><th>KWH</th><th>KVAH</th><th>V(R)</th></tr>
    </thead>
    <tbody>
      <tr><td>23/06/2026 23:30</td><td>48438.74</td><td>52313.84</td><td>226</td></tr>
      <tr><td>24/06/2026 00:00</td><td>48439.16</td><td>52314.29</td><td>229</td></tr>
      <tr><td>24/06/2026 00:30</td><td>48439.58</td><td>52314.75</td><td>220</td></tr>
    </tbody>
  </table>
</section>
"""

# ── Transformers list ────────────────────────────────────────────────
# Observed: single page, no pagination, ~40 DTs.
# Columns: CODE, NAME, FEEDER, CAPACITY (KVA)
# "Export all meters" button on this page.

TRANSFORMERS_LIST_HTML = """\
<table>
  <thead>
    <tr><th>CODE</th><th>NAME</th><th>FEEDER</th><th>CAPACITY (KVA)</th></tr>
  </thead>
  <tbody>
    <tr><td>DT-001</td><td>Malviya Nagar DT 1</td><td>F-001</td><td>100</td></tr>
    <tr><td>DT-002</td><td>Mansarovar DT 2</td><td>F-002</td><td>63</td></tr>
    <tr><td>DT-003</td><td>Vaishali DT 3</td><td>F-003</td><td>250</td></tr>
  </tbody>
</table>
"""

# ── Export CSV (from "Export all meters" button) ─────────────────────
# ASSUMPTION: Export produces CSV with all meters and basic columns.
# Button href observed but exact CSV format needs confirmation.

EXPORT_CSV_SAMPLE = """\
meter_id,serial_no,make,phase,status,dt_code,latitude,longitude
J100000,SE33962,HPL,single,Decommissioned,DT-001,26.938961002479868,75.83095696146852
J100001,GE84132,L&T,single,Installed,DT-002,26.940123,75.832456
J100002,AL28136,L&T,single,Installed,DT-003,26.941789,75.834012
"""
