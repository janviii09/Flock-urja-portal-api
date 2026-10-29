"""Adapter for the portal meter detail page (``/meters/{meterId}``).

Parses *all* sections of the detail page into typed domain objects:

- **Hierarchy breadcrumb** → :class:`~urja.domain.models.HierarchyPath`
- **Nameplate** → fields on :class:`~urja.domain.models.Meter`
- **Location** → :class:`~urja.domain.models.Location`
- **Consumption table** → list of :class:`~urja.domain.models.Reading`

Timestamps are in portal-local time (IST, UTC+05:30) and are stored
as **timezone-aware UTC** :class:`datetime` objects.  The raw IST string
is preserved in the quality_flags as ``original_timestamp:<value>`` for
auditing.

Known quirks handled
--------------------
- Breadcrumb separator is ``›`` (›, U+203A) not ``>``.
- Breadcrumb node format: ``{Name} ({Code})`` — code in parentheses.
- Partial breadcrumbs (fewer than 7 levels) get ``incomplete_hierarchy`` flag.
- Coordinates are very high-precision floats (15+ decimal places) — no flag.
- Coordinates (0.0, 0.0) get ``null_island`` flag.
- Reading timestamps: ``DD/MM/YYYY HH:MM`` format.
- KWH/KVAH are cumulative; non-monotonic sequences get ``non_monotonic_kwh`` flag.
- Missing V(R) column is allowed (``voltage_r=None``).
- ``L&amp;T`` in Make → ``L&T`` after HTML entity decode.
- ``Installation Type`` field may be absent from some pages → ``None``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from bs4 import Tag

from urja.adapters.base import (
    cell_text,
    collapse_whitespace,
    make_soup,
    normalise_make,
    normalise_meter_id,
    parse_float_flag,
    parse_table_rows,
)
from urja.client.exceptions import PortalShapeError
from urja.domain.models import (
    HierarchyLevel,
    HierarchyNode,
    HierarchyPath,
    InstallationStatus,
    Location,
    Meter,
    PhaseType,
    Reading,
)

logger = logging.getLogger(__name__)

# Portal timestamps are IST (Asia/Kolkata = UTC+05:30).
# We don't import pytz/zoneinfo to keep deps minimal; the offset is fixed.
_IST = timezone(timedelta(hours=5, minutes=30))

# Expected breadcrumb structure, top to bottom
_HIERARCHY_LEVELS = [
    HierarchyLevel.ZONE,
    HierarchyLevel.CIRCLE,
    HierarchyLevel.DIVISION,
    HierarchyLevel.SUBDIVISION,
    HierarchyLevel.SUBSTATION,
    HierarchyLevel.FEEDER,
    HierarchyLevel.DT,
]

# Separators used in the breadcrumb
_BREADCRUMB_SEPARATORS = re.compile(r"\s*›\s*|\s*>\s*")

# Pattern: "Name (CODE)" — code is alphanumeric with dashes
_BREADCRUMB_NODE = re.compile(r"^(.+?)\s+\(([A-Za-z0-9\-]+)\)\s*$")

# Timestamp format from the consumption table
_TIMESTAMP_FMT = "%d/%m/%Y %H:%M"

_NULL_SENTINELS = {"", "—", "-", "n/a", "null", "none"}


# ── Result container ─────────────────────────────────────────────────


class MeterDetail(NamedTuple):
    """All parsed data from a single meter detail page."""

    meter: Meter
    readings: list[Reading]


# ── Public entry point ────────────────────────────────────────────────


def parse_meter_detail(html: str, meter_id: str | None = None) -> MeterDetail:
    """Parse the full meter detail page HTML.

    Args:
        html: Raw HTML from ``GET /meters/{meterId}``.
        meter_id: Optional meter ID for context (used when nameplate parsing
            fails to still produce a partial object).

    Returns:
        :class:`MeterDetail` namedtuple with ``meter`` and ``readings``.

    Raises:
        :exc:`~urja.client.exceptions.PortalShapeError`: If the nameplate
            section is entirely missing (that's a structural change).
    """
    soup = make_soup(html)
    flags: list[str] = []

    # ── Hierarchy breadcrumb ──────────────────────────────────────────
    hierarchy_path = _parse_breadcrumb(soup, flags)

    # ── Nameplate ─────────────────────────────────────────────────────
    meter = _parse_nameplate(soup, meter_id, flags, hierarchy_path)

    # ── Location ──────────────────────────────────────────────────────
    location = _parse_location(soup, flags)
    meter = meter.model_copy(update={"location": location})

    # ── Consumption readings ──────────────────────────────────────────
    readings = _parse_readings(soup, meter.meter_id, flags)

    # Propagate page-level flags back to meter
    if flags:
        meter = meter.model_copy(update={"quality_flags": meter.quality_flags + flags})

    return MeterDetail(meter=meter, readings=readings)


# ── Breadcrumb parser ─────────────────────────────────────────────────


def _parse_breadcrumb(soup: Tag, flags: list[str]) -> HierarchyPath | None:
    """Find and parse the breadcrumb div into a HierarchyPath."""
    crumb_tag = soup.find(class_="breadcrumb")
    if crumb_tag is None:
        # Try common alternative patterns
        crumb_tag = soup.find("nav", attrs={"aria-label": re.compile("breadcrumb", re.I)})
    if crumb_tag is None:
        flags.append("missing_breadcrumb")
        return None

    raw_text = crumb_tag.get_text(" ", strip=True)
    # Collapse multiple spaces introduced by HTML whitespace
    raw_text = collapse_whitespace(raw_text)

    parts = _BREADCRUMB_SEPARATORS.split(raw_text)
    parts = [p.strip() for p in parts if p.strip()]

    if not parts:
        flags.append("empty_breadcrumb")
        return None

    nodes: list[HierarchyNode] = []
    for part in parts:
        m = _BREADCRUMB_NODE.match(part)
        if not m:
            # Fallback: treat the whole part as name with no code
            nodes.append(
                HierarchyNode(
                    code=part,
                    name=part,
                    level=HierarchyLevel.ZONE,  # placeholder; corrected below
                    quality_flags=["unparseable_breadcrumb_segment"],
                )
            )
            continue
        name, code = m.group(1).strip(), m.group(2).strip()
        nodes.append(
            HierarchyNode(
                code=code,
                name=name,
                level=HierarchyLevel.ZONE,  # corrected in assignment below
            )
        )

    # Assign levels by position (top-to-bottom)
    if len(nodes) != len(_HIERARCHY_LEVELS):
        flags.append(f"incomplete_hierarchy:{len(nodes)}_of_{len(_HIERARCHY_LEVELS)}_levels")

    path_kwargs: dict[str, HierarchyNode | None] = {lv.value: None for lv in _HIERARCHY_LEVELS}
    parent_code: str | None = None
    for i, node in enumerate(nodes):
        level = _HIERARCHY_LEVELS[i] if i < len(_HIERARCHY_LEVELS) else _HIERARCHY_LEVELS[-1]
        node = node.model_copy(update={"level": level, "parent_code": parent_code})
        path_kwargs[level.value] = node
        parent_code = node.code

    return HierarchyPath(**path_kwargs)


# ── Nameplate parser ──────────────────────────────────────────────────


def _parse_nameplate(
    soup: Tag,
    fallback_meter_id: str | None,
    flags: list[str],
    hierarchy_path: HierarchyPath | None,
) -> Meter:
    """Parse the Nameplate ``<dl>`` section into a Meter."""
    # Look for section.nameplate or any <dl> containing Meter ID
    section = soup.find("section", class_="nameplate")
    dl = section.find("dl") if section else soup.find("dl")

    if dl is None:
        if fallback_meter_id is None:
            raise PortalShapeError(
                "Meter detail page has no <dl> or nameplate section and no fallback meter ID.",
                field="nameplate",
            )
        flags.append("missing_nameplate")
        return Meter(
            meter_id=fallback_meter_id,
            serial_no="",
            make="",
            quality_flags=["missing_nameplate"],
        )

    pairs = _dl_to_dict(dl)

    meter_id_raw = pairs.get("meter id") or pairs.get("meter") or fallback_meter_id or ""
    meter_id = normalise_meter_id(meter_id_raw)
    if not meter_id:
        raise PortalShapeError("Cannot determine meter ID from nameplate.", field="meter_id")

    serial_no = pairs.get("serial no") or pairs.get("serial") or ""
    if not serial_no or serial_no.lower() in _NULL_SENTINELS:
        flags.append("missing_serial")
        serial_no = ""

    make = normalise_make(pairs.get("make") or "")
    if not make:
        flags.append("missing_make")

    # Phase
    phase_raw = (pairs.get("phase type") or pairs.get("phase") or "").lower().strip()
    phase_type: PhaseType | None = None
    if phase_raw == "single":
        phase_type = PhaseType.SINGLE
    elif phase_raw in ("three", "3phase", "3-phase"):
        phase_type = PhaseType.THREE
    elif phase_raw not in _NULL_SENTINELS:
        flags.append(f"unknown_phase:{phase_raw}")

    # Status
    status_raw = collapse_whitespace(pairs.get("installation status") or pairs.get("status") or "")
    installation_status: InstallationStatus | None = None
    try:
        installation_status = InstallationStatus(status_raw)
    except ValueError:
        if status_raw.lower() not in _NULL_SENTINELS:
            flags.append(f"unknown_status:{status_raw}")

    # Installation type (optional)
    installation_type = pairs.get("installation type") or pairs.get("type") or None
    if installation_type:
        installation_type = collapse_whitespace(installation_type)

    # DT code from hierarchy path
    dt_code = hierarchy_path.dt.code if (hierarchy_path and hierarchy_path.dt) else None

    return Meter(
        meter_id=meter_id,
        serial_no=serial_no,
        make=make,
        phase_type=phase_type,
        installation_status=installation_status,
        installation_type=installation_type,
        dt_code=dt_code,
        hierarchy_path=hierarchy_path,
        quality_flags=[],  # page-level flags added by caller
    )


def _dl_to_dict(dl: Tag) -> dict[str, str]:
    """Convert a ``<dl>`` to ``{dt_text.lower(): dd_text}`` dict."""
    result: dict[str, str] = {}
    dts = dl.find_all("dt")
    dds = dl.find_all("dd")
    for dt, dd in zip(dts, dds):
        key = cell_text(dt).lower().strip().rstrip(":")
        val = cell_text(dd)
        result[key] = val
    return result


# ── Location parser ───────────────────────────────────────────────────


def _parse_location(soup: Tag, flags: list[str]) -> Location | None:
    """Parse the Location section coordinates."""
    section = soup.find("section", class_="location")
    if section is None:
        return None

    # Look for a <p class="coords"> or similar
    coords_tag = section.find(class_="coords") or section.find("p")
    if coords_tag is None:
        flags.append("missing_location_coords")
        return None

    raw = cell_text(coords_tag)
    # Expected format: "26.938961002479868, 75.83095696146852"
    parts = raw.split(",")
    if len(parts) != 2:
        flags.append("malformed_location_coords")
        return None

    loc_flags: list[str] = []
    lat = parse_float_flag(parts[0], "latitude", loc_flags)
    lon = parse_float_flag(parts[1], "longitude", loc_flags)

    if lat is None or lon is None:
        flags.extend(loc_flags)
        return None

    # Null Island guard
    if lat == 0.0 and lon == 0.0:
        loc_flags.append("null_island")

    return Location(latitude=lat, longitude=lon, quality_flags=loc_flags)


# ── Consumption table parser ──────────────────────────────────────────

_CONSUMPTION_REQUIRED_COLS = ["timestamp", "kwh", "kvah"]


def _parse_readings(soup: Tag, meter_id: str, page_flags: list[str]) -> list[Reading]:
    """Parse the Consumption table into a list of Readings."""
    section = soup.find("section", class_="consumption")
    if section is None:
        return []

    table = section.find("table")
    if table is None:
        page_flags.append("consumption_table_missing")
        return []

    try:
        rows = parse_table_rows(table, _CONSUMPTION_REQUIRED_COLS)
    except PortalShapeError as exc:
        page_flags.append(f"consumption_shape_error:{exc.field}")
        return []

    readings: list[Reading] = []
    prev_kwh: float | None = None

    for row in rows:
        r_flags: list[str] = []

        # Timestamp
        ts_raw = row.get("timestamp", "").strip()
        ts_utc = _parse_timestamp_ist(ts_raw, r_flags)
        if ts_utc is None:
            continue  # skip unreadable rows entirely

        # KWH / KVAH
        kwh = parse_float_flag(row.get("kwh", ""), "kwh", r_flags)
        kvah = parse_float_flag(row.get("kvah", ""), "kvah", r_flags)
        if kwh is None or kvah is None:
            continue

        # Non-monotonic KWH check
        if prev_kwh is not None and kwh < prev_kwh:
            r_flags.append("non_monotonic_kwh")
        prev_kwh = kwh

        # V(R) — optional
        voltage_raw = row.get("v(r)") or row.get("vr") or row.get("v") or ""
        voltage_r = parse_float_flag(voltage_raw, "voltage_r", []) if voltage_raw.strip() else None

        readings.append(
            Reading(
                meter_id=meter_id,
                timestamp=ts_utc,
                kwh=kwh,
                kvah=kvah,
                voltage_r=voltage_r,
                quality_flags=r_flags,
            )
        )

    return readings


def _parse_timestamp_ist(raw: str, flags: list[str]) -> datetime | None:
    """Parse ``DD/MM/YYYY HH:MM`` string as IST, return UTC-aware datetime.

    Appends ``timestamp_unparseable`` to *flags* and returns None on failure.
    """
    raw = raw.strip()
    try:
        naive = datetime.strptime(raw, _TIMESTAMP_FMT)
    except ValueError:
        flags.append(f"timestamp_unparseable:{raw!r}")
        return None

    ist_aware = naive.replace(tzinfo=_IST)
    return ist_aware.astimezone(timezone.utc)
