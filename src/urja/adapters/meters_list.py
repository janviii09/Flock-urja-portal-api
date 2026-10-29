"""Adapter for the portal meters list page (``/meters``).

Parses the paginated HTML table and produces a list of :class:`~urja.domain.models.Meter`
objects containing list-page-level data (no detail, no location, no readings).

Portal table columns (observed): METER, SERIAL, MAKE, PHASE, STATUS, DT

Known quirks handled
--------------------
- ``L&amp;T`` HTML entity in MAKE → normalised to ``L&T``.
- Meter IDs are J-prefixed with 6-digit zero-padding: ``J100000``—``J100402``.
- STATUS values not in the known enum get ``unknown_status`` quality flag.
- PHASE values not in the known enum get ``unknown_phase`` quality flag.
- Empty SERIAL (``—`` dash or blank) gets ``missing_serial`` quality flag.
- DT code missing gets ``missing_dt`` quality flag.
"""

from __future__ import annotations

import logging

from urja.adapters.base import (
    collapse_whitespace,
    make_soup,
    normalise_make,
    normalise_meter_id,
    parse_table_rows,
)
from urja.client.exceptions import PortalShapeError
from urja.domain.models import InstallationStatus, Meter, PhaseType

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

_REQUIRED_COLUMNS = ["meter", "serial", "make", "phase", "status", "dt"]

# Values the portal renders for a missing/unknown value
_NULL_SENTINELS = {"", "—", "-", "n/a", "null", "none"}


def parse_meters_list(html: str) -> list[Meter]:
    """Parse the meters list page HTML and return a list of :class:`Meter`.

    Args:
        html: Raw HTML from ``GET /meters`` (any page).

    Returns:
        List of Meter objects, one per table row.

    Raises:
        :exc:`~urja.client.exceptions.PortalShapeError`: If the expected
            table or required columns are absent.
    """
    soup = make_soup(html)

    table = soup.find("table")
    if table is None:
        raise PortalShapeError(
            "Meters list page has no <table> element. Portal may have changed.",
            field="table",
        )

    rows = parse_table_rows(table, _REQUIRED_COLUMNS)
    if not rows:
        logger.debug("Meters list table has zero data rows.")
        return []

    meters: list[Meter] = []
    for row in rows:
        flags: list[str] = []

        raw_id = row.get("meter", "")
        meter_id = normalise_meter_id(raw_id)
        if not meter_id:
            logger.warning("Skipping row with empty meter ID: %s", row)
            continue

        # SERIAL
        serial_raw = row.get("serial", "").strip()
        serial_no = serial_raw if serial_raw.lower() not in _NULL_SENTINELS else ""
        if not serial_no:
            flags.append("missing_serial")

        # MAKE — unescape HTML entity
        make = normalise_make(row.get("make", ""))
        if not make:
            flags.append("missing_make")

        # PHASE
        phase_raw = row.get("phase", "").strip().lower()
        phase_type: PhaseType | None = None
        if phase_raw == "single":
            phase_type = PhaseType.SINGLE
        elif phase_raw in ("three", "3phase", "3-phase"):
            phase_type = PhaseType.THREE
        elif phase_raw not in _NULL_SENTINELS:
            flags.append(f"unknown_phase:{phase_raw}")
        else:
            flags.append("missing_phase")

        # STATUS
        status_raw = collapse_whitespace(row.get("status", ""))
        installation_status: InstallationStatus | None = None
        try:
            installation_status = InstallationStatus(status_raw)
        except ValueError:
            if status_raw.lower() not in _NULL_SENTINELS:
                flags.append(f"unknown_status:{status_raw}")
            else:
                flags.append("missing_status")

        # DT
        dt_raw = row.get("dt", "").strip()
        dt_code = dt_raw if dt_raw.lower() not in _NULL_SENTINELS else None
        if not dt_code:
            flags.append("missing_dt")

        meters.append(
            Meter(
                meter_id=meter_id,
                serial_no=serial_no,
                make=make,
                phase_type=phase_type,
                installation_status=installation_status,
                dt_code=dt_code,
                quality_flags=flags,
            )
        )

    return meters


def parse_total_count(html: str) -> int | None:
    """Extract the total meter count shown in the page header, if present.

    The portal shows e.g. ``403 total`` somewhere above or near the table.
    Returns ``None`` if the count cannot be found (not a hard error).
    """
    import re

    soup = make_soup(html)
    text = soup.get_text(" ", strip=True)
    m = re.search(r"(\d+)\s+total", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def parse_pagination(html: str) -> tuple[int, int]:
    """Return ``(current_page, total_pages)`` from the pagination widget.

    The portal renders: ``Page X of Y``.
    Returns ``(1, 1)`` if pagination cannot be found.
    """
    import re

    soup = make_soup(html)
    text = soup.get_text(" ", strip=True)
    m = re.search(r"[Pp]age\s+(\d+)\s+of\s+(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1, 1
