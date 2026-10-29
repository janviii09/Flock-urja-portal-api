"""Tests for Phase 3 adapters.

One class per adapter:
  - TestMetersListAdapter
  - TestMeterDetailAdapter
  - TestTransformersAdapter
  - TestBreadcrumbParser (unit-level)
  - TestTimestampConversion (unit-level)

Each known quirk has its own dedicated test method.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from urja.adapters import (
    MeterDetail,
    parse_meter_detail,
    parse_meters_list,
    parse_pagination,
    parse_total_count,
    parse_transformers,
)
from urja.client.exceptions import PortalShapeError
from urja.domain.models import (
    HierarchyLevel,
    InstallationStatus,
    PhaseType,
)

# ═══════════════════════════════════════════════════════════════════════
# HTML FIXTURES — sanitised representations of observed portal pages
# ═══════════════════════════════════════════════════════════════════════

# ── Meters list fixtures ──────────────────────────────────────────────

METERS_LIST_NORMAL = """\
<html><body>
<p>403 total</p>
<table>
  <thead><tr>
    <th>METER</th><th>SERIAL</th><th>MAKE</th>
    <th>PHASE</th><th>STATUS</th><th>DT</th>
  </tr></thead>
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
      <td>AL28136</td><td>Genus</td><td>three</td>
      <td>Faulty</td><td>DT-003</td>
    </tr>
  </tbody>
</table>
<div class="pagination">
  <button disabled>Previous</button>
  <span>Page 1 of 21</span>
  <button>Next</button>
</div>
</body></html>
"""

METERS_LIST_QUIRKS = """\
<html><body>
<table>
  <thead><tr>
    <th>METER</th><th>SERIAL</th><th>MAKE</th>
    <th>PHASE</th><th>STATUS</th><th>DT</th>
  </tr></thead>
  <tbody>
    <tr>
      <!-- missing serial (em-dash sentinel) -->
      <td>J100010</td><td>—</td><td>HPL</td>
      <td>single</td><td>Installed</td><td>DT-005</td>
    </tr>
    <tr>
      <!-- unknown phase -->
      <td>J100011</td><td>HP12345</td><td>Secure</td>
      <td>dc</td><td>Installed</td><td>DT-005</td>
    </tr>
    <tr>
      <!-- unknown status -->
      <td>J100012</td><td>HP22222</td><td>Allied</td>
      <td>single</td><td>Retired</td><td>DT-006</td>
    </tr>
    <tr>
      <!-- missing DT -->
      <td>J100013</td><td>HP33333</td><td>HPL</td>
      <td>three</td><td>Faulty</td><td>—</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

METERS_LIST_NO_TABLE = "<html><body><p>No data.</p></body></html>"

METERS_LIST_MISSING_COLUMN = """\
<html><body>
<table>
  <thead><tr>
    <th>METER</th><th>SERIAL</th><th>MAKE</th><th>PHASE</th><th>STATUS</th>
    <!-- DT column is gone -->
  </tr></thead>
  <tbody>
    <tr><td>J100000</td><td>SE33962</td><td>HPL</td><td>single</td><td>Installed</td></tr>
  </tbody>
</table>
</body></html>
"""

# ── Meter detail fixtures ─────────────────────────────────────────────

METER_DETAIL_NORMAL = """\
<html><body>
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
    <thead><tr><th>TIMESTAMP</th><th>KWH</th><th>KVAH</th><th>V(R)</th></tr></thead>
    <tbody>
      <tr><td>23/06/2026 23:30</td><td>48438.74</td><td>52313.84</td><td>226</td></tr>
      <tr><td>24/06/2026 00:00</td><td>48439.16</td><td>52314.29</td><td>229</td></tr>
      <tr><td>24/06/2026 00:30</td><td>48439.58</td><td>52314.75</td><td>220</td></tr>
    </tbody>
  </table>
</section>
</body></html>
"""

METER_DETAIL_LT_MAKE = """\
<html><body>
<div class="breadcrumb">
  Zone 1 (Z-01) › Circle 1 (C-01) › Division 1 (D-01) ›
  Subdivision 1 (SD-01) › Substation 1 (SS-01) › Feeder 1 (F-001) ›
  DT 1 (DT-002)
</div>
<section class="nameplate">
  <dl>
    <dt>Meter ID</dt><dd>J100001</dd>
    <dt>Serial No</dt><dd>GE84132</dd>
    <dt>Make</dt><dd>L&amp;T</dd>
    <dt>Phase Type</dt><dd>single</dd>
    <dt>Installation Status</dt><dd>Installed</dd>
  </dl>
</section>
<section class="location">
  <p class="coords">26.940123, 75.832456</p>
</section>
<section class="consumption">
  <table>
    <thead><tr><th>TIMESTAMP</th><th>KWH</th><th>KVAH</th><th>V(R)</th></tr></thead>
    <tbody>
      <tr><td>24/06/2026 01:00</td><td>1000.0</td><td>1100.0</td><td>230</td></tr>
    </tbody>
  </table>
</section>
</body></html>
"""

METER_DETAIL_NULL_ISLAND = """\
<html><body>
<div class="breadcrumb">Zone 1 (Z-01) › Circle 1 (C-01) › Division 1 (D-01) ›
  Subdivision 1 (SD-01) › Substation 1 (SS-01) › Feeder 1 (F-001) › DT 1 (DT-001)</div>
<section class="nameplate">
  <dl>
    <dt>Meter ID</dt><dd>J100020</dd>
    <dt>Serial No</dt><dd>SE00001</dd>
    <dt>Make</dt><dd>HPL</dd>
    <dt>Phase Type</dt><dd>single</dd>
    <dt>Installation Status</dt><dd>Installed</dd>
  </dl>
</section>
<section class="location">
  <p class="coords">0.0, 0.0</p>
</section>
<section class="consumption">
  <table>
    <thead><tr><th>TIMESTAMP</th><th>KWH</th><th>KVAH</th><th>V(R)</th></tr></thead>
    <tbody></tbody>
  </table>
</section>
</body></html>
"""

METER_DETAIL_NON_MONOTONIC = """\
<html><body>
<div class="breadcrumb">Zone 1 (Z-01) › Circle 1 (C-01) › Division 1 (D-01) ›
  Subdivision 1 (SD-01) › Substation 1 (SS-01) › Feeder 2 (F-002) › DT 2 (DT-002)</div>
<section class="nameplate">
  <dl>
    <dt>Meter ID</dt><dd>J100030</dd>
    <dt>Serial No</dt><dd>AL55555</dd>
    <dt>Make</dt><dd>Allied</dd>
    <dt>Phase Type</dt><dd>single</dd>
    <dt>Installation Status</dt><dd>Installed</dd>
  </dl>
</section>
<section class="location">
  <p class="coords">26.9, 75.8</p>
</section>
<section class="consumption">
  <table>
    <thead><tr><th>TIMESTAMP</th><th>KWH</th><th>KVAH</th><th>V(R)</th></tr></thead>
    <tbody>
      <tr><td>24/06/2026 01:00</td><td>5000.0</td><td>5500.0</td><td>225</td></tr>
      <tr><td>24/06/2026 01:30</td><td>4999.0</td><td>5501.0</td><td>224</td></tr>
      <tr><td>24/06/2026 02:00</td><td>5001.0</td><td>5502.0</td><td>226</td></tr>
    </tbody>
  </table>
</section>
</body></html>
"""

METER_DETAIL_MISSING_NAMEPLATE = """\
<html><body>
<div class="breadcrumb">Zone 1 (Z-01) › Circle 1 (C-01) › Division 1 (D-01) ›
  Subdivision 1 (SD-01) › Substation 1 (SS-01) › Feeder 1 (F-001) › DT 1 (DT-001)</div>
<!-- no <section class="nameplate">, no <dl> -->
<section class="consumption">
  <table>
    <thead><tr><th>TIMESTAMP</th><th>KWH</th><th>KVAH</th></tr></thead>
    <tbody></tbody>
  </table>
</section>
</body></html>
"""

METER_DETAIL_INCOMPLETE_BREADCRUMB = """\
<html><body>
<div class="breadcrumb">
  Zone 1 (Z-01) › Circle 1 (C-01)
</div>
<section class="nameplate">
  <dl>
    <dt>Meter ID</dt><dd>J100040</dd>
    <dt>Serial No</dt><dd>SE99999</dd>
    <dt>Make</dt><dd>Genus</dd>
    <dt>Phase Type</dt><dd>three</dd>
    <dt>Installation Status</dt><dd>Faulty</dd>
  </dl>
</section>
<section class="location">
  <p class="coords">26.93, 75.83</p>
</section>
<section class="consumption">
  <table>
    <thead><tr><th>TIMESTAMP</th><th>KWH</th><th>KVAH</th></tr></thead>
    <tbody></tbody>
  </table>
</section>
</body></html>
"""

METER_DETAIL_NO_VOLTAGE_COL = """\
<html><body>
<div class="breadcrumb">Zone 1 (Z-01) › Circle 1 (C-01) › Division 1 (D-01) ›
  Subdivision 1 (SD-01) › Substation 1 (SS-01) › Feeder 1 (F-001) › DT 1 (DT-001)</div>
<section class="nameplate">
  <dl>
    <dt>Meter ID</dt><dd>J100050</dd>
    <dt>Serial No</dt><dd>SE77777</dd>
    <dt>Make</dt><dd>HPL</dd>
    <dt>Phase Type</dt><dd>single</dd>
    <dt>Installation Status</dt><dd>Installed</dd>
  </dl>
</section>
<section class="location"><p class="coords">26.93, 75.83</p></section>
<section class="consumption">
  <table>
    <thead><tr><th>TIMESTAMP</th><th>KWH</th><th>KVAH</th></tr></thead>
    <tbody>
      <tr><td>25/06/2026 08:00</td><td>200.0</td><td>210.0</td></tr>
    </tbody>
  </table>
</section>
</body></html>
"""

# ── Transformers fixtures ─────────────────────────────────────────────

TRANSFORMERS_NORMAL = """\
<html><body>
<table>
  <thead><tr>
    <th>CODE</th><th>NAME</th><th>FEEDER</th><th>CAPACITY (KVA)</th>
  </tr></thead>
  <tbody>
    <tr><td>DT-001</td><td>Malviya Nagar DT 1</td><td>F-001</td><td>100</td></tr>
    <tr><td>DT-002</td><td>Mansarovar DT 2</td><td>F-002</td><td>63</td></tr>
    <tr><td>DT-003</td><td>Vaishali DT 3</td><td>F-003</td><td>250</td></tr>
  </tbody>
</table>
</body></html>
"""

TRANSFORMERS_BAD_CAPACITY = """\
<html><body>
<table>
  <thead><tr>
    <th>CODE</th><th>NAME</th><th>FEEDER</th><th>CAPACITY (KVA)</th>
  </tr></thead>
  <tbody>
    <tr><td>DT-010</td><td>Unknown DT</td><td>F-010</td><td>N/A</td></tr>
    <tr><td>DT-011</td><td>Good DT</td><td>F-011</td><td>400</td></tr>
  </tbody>
</table>
</body></html>
"""

TRANSFORMERS_NO_TABLE = "<html><body><p>Maintenance mode.</p></body></html>"

TRANSFORMERS_MISSING_COLUMN = """\
<html><body>
<table>
  <thead><tr>
    <th>CODE</th><th>NAME</th><th>FEEDER</th>
    <!-- CAPACITY (KVA) is gone -->
  </tr></thead>
  <tbody>
    <tr><td>DT-001</td><td>Malviya Nagar DT 1</td><td>F-001</td></tr>
  </tbody>
</table>
</body></html>
"""


# ═══════════════════════════════════════════════════════════════════════
# METERS LIST TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestMetersListAdapter:
    def test_normal_parse_returns_all_rows(self):
        """Happy-path: 3 rows → 3 Meter objects with correct fields."""
        meters = parse_meters_list(METERS_LIST_NORMAL)
        assert len(meters) == 3

    def test_meter_ids_uppercased(self):
        meters = parse_meters_list(METERS_LIST_NORMAL)
        assert all(m.meter_id == m.meter_id.upper() for m in meters)

    def test_first_meter_fields(self):
        meters = parse_meters_list(METERS_LIST_NORMAL)
        m = meters[0]
        assert m.meter_id == "J100000"
        assert m.serial_no == "SE33962"
        assert m.make == "HPL"
        assert m.phase_type == PhaseType.SINGLE
        assert m.installation_status == InstallationStatus.DECOMMISSIONED
        assert m.dt_code == "DT-001"
        assert m.quality_flags == []

    def test_lt_make_html_entity_decoded(self):
        """L&amp;T in HTML must be decoded to L&T in the domain model."""
        meters = parse_meters_list(METERS_LIST_NORMAL)
        lt_meter = next(m for m in meters if m.meter_id == "J100001")
        assert lt_meter.make == "L&T"

    def test_three_phase_parsed(self):
        meters = parse_meters_list(METERS_LIST_NORMAL)
        three_phase = next(m for m in meters if m.meter_id == "J100002")
        assert three_phase.phase_type == PhaseType.THREE
        assert three_phase.installation_status == InstallationStatus.FAULTY

    def test_missing_serial_em_dash_flagged(self):
        """Em-dash sentinel in SERIAL column → missing_serial flag, empty serial."""
        meters = parse_meters_list(METERS_LIST_QUIRKS)
        m = next(m for m in meters if m.meter_id == "J100010")
        assert m.serial_no == ""
        assert "missing_serial" in m.quality_flags

    def test_unknown_phase_flagged(self):
        """Unknown phase value → flag with raw value, phase_type=None."""
        meters = parse_meters_list(METERS_LIST_QUIRKS)
        m = next(m for m in meters if m.meter_id == "J100011")
        assert m.phase_type is None
        assert any("unknown_phase" in f for f in m.quality_flags)

    def test_unknown_status_flagged(self):
        """Unknown status value → flag with raw value, installation_status=None."""
        meters = parse_meters_list(METERS_LIST_QUIRKS)
        m = next(m for m in meters if m.meter_id == "J100012")
        assert m.installation_status is None
        assert any("unknown_status" in f for f in m.quality_flags)

    def test_missing_dt_em_dash_flagged(self):
        """Em-dash sentinel in DT column → missing_dt flag, dt_code=None."""
        meters = parse_meters_list(METERS_LIST_QUIRKS)
        m = next(m for m in meters if m.meter_id == "J100013")
        assert m.dt_code is None
        assert "missing_dt" in m.quality_flags

    def test_no_table_raises_shape_error(self):
        """No <table> element → PortalShapeError with field='table'."""
        with pytest.raises(PortalShapeError) as exc_info:
            parse_meters_list(METERS_LIST_NO_TABLE)
        assert exc_info.value.field == "table"

    def test_missing_column_raises_shape_error(self):
        """Missing required column → PortalShapeError naming the column."""
        with pytest.raises(PortalShapeError) as exc_info:
            parse_meters_list(METERS_LIST_MISSING_COLUMN)
        assert exc_info.value.field is not None
        assert "dt" in exc_info.value.field.lower()

    def test_total_count_extracted(self):
        assert parse_total_count(METERS_LIST_NORMAL) == 403

    def test_total_count_none_when_absent(self):
        assert parse_total_count(METERS_LIST_QUIRKS) is None

    def test_pagination_extracted(self):
        current, total = parse_pagination(METERS_LIST_NORMAL)
        assert current == 1
        assert total == 21

    def test_pagination_fallback_when_absent(self):
        current, total = parse_pagination(METERS_LIST_QUIRKS)
        assert current == 1
        assert total == 1


# ═══════════════════════════════════════════════════════════════════════
# METER DETAIL TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestMeterDetailAdapter:
    def test_normal_parse_returns_meter_detail(self):
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        assert isinstance(result, MeterDetail)

    def test_nameplate_fields(self):
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        m = result.meter
        assert m.meter_id == "J100000"
        assert m.serial_no == "SE33962"
        assert m.make == "HPL"
        assert m.phase_type == PhaseType.SINGLE
        assert m.installation_status == InstallationStatus.DECOMMISSIONED
        assert m.installation_type == "Whole Current"

    def test_location_parsed(self):
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        loc = result.meter.location
        assert loc is not None
        assert abs(loc.latitude - 26.938961002479868) < 1e-9
        assert abs(loc.longitude - 75.83095696146852) < 1e-9
        assert loc.quality_flags == []

    def test_hierarchy_breadcrumb_all_seven_levels(self):
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        path = result.meter.hierarchy_path
        assert path is not None
        assert path.zone.code == "Z-01"
        assert path.zone.name == "Jaipur Zone 1"
        assert path.zone.level == HierarchyLevel.ZONE
        assert path.circle.code == "C-01"
        assert path.division.code == "D-01"
        assert path.subdivision.code == "SD-01"
        assert path.substation.code == "SS-01"
        assert path.feeder.code == "F-001"
        assert path.dt.code == "DT-001"
        assert path.dt.name == "Malviya Nagar DT 1"

    def test_hierarchy_parent_chain(self):
        """Each node's parent_code points to the node above it."""
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        path = result.meter.hierarchy_path
        assert path.zone.parent_code is None
        assert path.circle.parent_code == "Z-01"
        assert path.dt.parent_code == "F-001"

    def test_dt_code_on_meter_derived_from_hierarchy(self):
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        assert result.meter.dt_code == "DT-001"

    def test_readings_count_and_order(self):
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        assert len(result.readings) == 3

    def test_readings_timestamps_converted_to_utc(self):
        """Portal timestamps are IST (UTC+05:30); readings must be UTC-aware."""
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        r = result.readings[0]
        assert r.timestamp.tzinfo == timezone.utc
        # 23/06/2026 23:30 IST = 23/06/2026 18:00 UTC
        assert r.timestamp == datetime(2026, 6, 23, 18, 0, tzinfo=timezone.utc)

    def test_readings_kwh_kvah_values(self):
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        r = result.readings[0]
        assert r.kwh == 48438.74
        assert r.kvah == 52313.84
        assert r.voltage_r == 226.0

    def test_readings_meter_id_propagated(self):
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        assert all(r.meter_id == "J100000" for r in result.readings)

    def test_lt_make_html_entity_decoded(self):
        """L&amp;T in nameplate Make → decoded to L&T."""
        result = parse_meter_detail(METER_DETAIL_LT_MAKE)
        assert result.meter.make == "L&T"

    def test_null_island_coordinates_flagged(self):
        """(0.0, 0.0) coordinates → null_island quality flag on Location."""
        result = parse_meter_detail(METER_DETAIL_NULL_ISLAND)
        loc = result.meter.location
        assert loc is not None
        assert loc.latitude == 0.0
        assert "null_island" in loc.quality_flags

    def test_non_monotonic_kwh_flagged(self):
        """KWH going backward → non_monotonic_kwh flag on that Reading."""
        result = parse_meter_detail(METER_DETAIL_NON_MONOTONIC)
        flagged = [r for r in result.readings if "non_monotonic_kwh" in r.quality_flags]
        assert len(flagged) == 1
        assert flagged[0].kwh == 4999.0

    def test_missing_nameplate_with_fallback_id(self):
        """When nameplate section is missing, use fallback meter_id kwarg."""
        result = parse_meter_detail(METER_DETAIL_MISSING_NAMEPLATE, meter_id="J100099")
        assert result.meter.meter_id == "J100099"
        assert "missing_nameplate" in result.meter.quality_flags

    def test_missing_nameplate_without_fallback_raises(self):
        """When nameplate is missing and no fallback ID given → PortalShapeError."""
        with pytest.raises(PortalShapeError) as exc_info:
            parse_meter_detail(METER_DETAIL_MISSING_NAMEPLATE)
        assert exc_info.value.field == "nameplate"

    def test_incomplete_breadcrumb_flagged(self):
        """Breadcrumb with fewer than 7 nodes → incomplete_hierarchy quality flag."""
        result = parse_meter_detail(METER_DETAIL_INCOMPLETE_BREADCRUMB)
        # Flag is on the page level, propagated to meter
        all_flags = result.meter.quality_flags
        assert any("incomplete_hierarchy" in f for f in all_flags)

    def test_missing_voltage_column_readings_have_none(self):
        """Consumption table without V(R) column → voltage_r=None on all readings."""
        result = parse_meter_detail(METER_DETAIL_NO_VOLTAGE_COL)
        assert len(result.readings) == 1
        assert result.readings[0].voltage_r is None
        assert result.readings[0].quality_flags == []

    def test_no_flags_on_clean_data(self):
        """Happy-path page with perfect data → no quality flags."""
        result = parse_meter_detail(METER_DETAIL_NORMAL)
        assert result.meter.quality_flags == []
        for r in result.readings:
            assert r.quality_flags == []

    def test_installation_type_optional(self):
        """Make sure a detail page without Installation Type field works."""
        result = parse_meter_detail(METER_DETAIL_LT_MAKE)
        assert result.meter.installation_type is None


# ═══════════════════════════════════════════════════════════════════════
# TRANSFORMERS TESTS
# ═══════════════════════════════════════════════════════════════════════


class TestTransformersAdapter:
    def test_normal_parse(self):
        transformers = parse_transformers(TRANSFORMERS_NORMAL)
        assert len(transformers) == 3

    def test_first_transformer_fields(self):
        transformers = parse_transformers(TRANSFORMERS_NORMAL)
        t = transformers[0]
        assert t.code == "DT-001"
        assert t.name == "Malviya Nagar DT 1"
        assert t.feeder == "F-001"
        assert t.capacity_kva == 100.0
        assert t.quality_flags == []

    def test_code_uppercased(self):
        transformers = parse_transformers(TRANSFORMERS_NORMAL)
        assert all(t.code == t.code.upper() for t in transformers)

    def test_capacity_kva_as_float(self):
        transformers = parse_transformers(TRANSFORMERS_NORMAL)
        assert transformers[1].capacity_kva == 63.0
        assert transformers[2].capacity_kva == 250.0

    def test_bad_capacity_flagged_not_raised(self):
        """Unparseable capacity → capacity_unparseable flag, sentinel 0.0, no raise."""
        transformers = parse_transformers(TRANSFORMERS_BAD_CAPACITY)
        bad = next(t for t in transformers if t.code == "DT-010")
        assert "capacity_unparseable" in bad.quality_flags
        assert bad.capacity_kva == 0.0

    def test_valid_row_after_bad_still_parsed(self):
        transformers = parse_transformers(TRANSFORMERS_BAD_CAPACITY)
        good = next(t for t in transformers if t.code == "DT-011")
        assert good.capacity_kva == 400.0
        assert good.quality_flags == []

    def test_no_table_raises_shape_error(self):
        with pytest.raises(PortalShapeError) as exc_info:
            parse_transformers(TRANSFORMERS_NO_TABLE)
        assert exc_info.value.field == "table"

    def test_missing_column_raises_shape_error(self):
        """Missing CAPACITY (KVA) column → PortalShapeError."""
        with pytest.raises(PortalShapeError) as exc_info:
            parse_transformers(TRANSFORMERS_MISSING_COLUMN)
        assert exc_info.value.field is not None
        assert "capacity" in exc_info.value.field.lower()


# ═══════════════════════════════════════════════════════════════════════
# UNIT — TIMESTAMP CONVERSION
# ═══════════════════════════════════════════════════════════════════════


class TestTimestampConversion:
    """Focused unit tests for IST → UTC conversion."""

    from urja.adapters.meter_detail import _parse_timestamp_ist

    def test_known_ist_to_utc(self):
        from urja.adapters.meter_detail import _parse_timestamp_ist

        flags: list[str] = []
        result = _parse_timestamp_ist("23/06/2026 23:30", flags)
        assert result is not None
        assert result == datetime(2026, 6, 23, 18, 0, tzinfo=timezone.utc)
        assert flags == []

    def test_midnight_ist_to_previous_day_utc(self):
        """00:00 IST is 18:30 UTC the previous day."""
        from urja.adapters.meter_detail import _parse_timestamp_ist

        flags: list[str] = []
        result = _parse_timestamp_ist("24/06/2026 00:00", flags)
        assert result == datetime(2026, 6, 23, 18, 30, tzinfo=timezone.utc)

    def test_invalid_timestamp_appends_flag(self):
        from urja.adapters.meter_detail import _parse_timestamp_ist

        flags: list[str] = []
        result = _parse_timestamp_ist("not-a-date", flags)
        assert result is None
        assert any("timestamp_unparseable" in f for f in flags)

    def test_wrong_format_flagged(self):
        """ISO format without DD/MM/YYYY → unparseable flag."""
        from urja.adapters.meter_detail import _parse_timestamp_ist

        flags: list[str] = []
        result = _parse_timestamp_ist("2026-06-23T23:30:00", flags)
        assert result is None
        assert any("timestamp_unparseable" in f for f in flags)
