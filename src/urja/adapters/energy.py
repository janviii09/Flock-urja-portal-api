"""Adapter for the /portal/meters/{id}/energy JSON endpoint.

Parses time series readings, preserves literal raw_timestamp, normalizes to UTC,
and flags data quality issues (non-monotonic registers, unparseable floats).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from urja.client.exceptions import PortalShapeError
from urja.domain.models import Reading

logger = logging.getLogger(__name__)

# Indian electricity utility timezone: Asia/Kolkata (IST, UTC+05:30)
_IST = ZoneInfo("Asia/Kolkata")


def parse_energy_payload(
    payload: dict[str, Any],
    meter_id: str,
) -> list[Reading]:
    """Parse JSON response from GET /portal/meters/{id}/energy.

    Args:
        payload: Decoded JSON dictionary.
        meter_id: Canonical meter identifier.

    Returns:
        List of Reading objects ordered chronologically.

    Raises:
        PortalShapeError: If payload lacks required 'data' list.
    """
    if (
        not isinstance(payload, dict)
        or "data" not in payload
        or not isinstance(payload["data"], list)
    ):
        raise PortalShapeError(
            f"Energy payload for meter {meter_id} missing 'data' array.",
            field="data",
        )

    raw_readings = payload["data"]
    readings: list[Reading] = []
    prev_kwh: float | None = None

    for item in raw_readings:
        if not isinstance(item, dict):
            continue

        raw_ts = str(item.get("timestamp", "")).strip()
        if not raw_ts:
            continue

        quality_flags: list[str] = []

        # 1. Parse timestamp (DD/MM/YYYY HH:mm) -> convert IST to UTC
        try:
            # Handle standard 16-char format "DD/MM/YYYY HH:mm"
            dt_local = datetime.strptime(raw_ts, "%d/%m/%Y %H:%M").replace(tzinfo=_IST)
            dt_utc = dt_local.astimezone(timezone.utc)
        except ValueError:
            try:
                # Handle possible daily date "DD/MM/YYYY"
                dt_local = datetime.strptime(raw_ts, "%d/%m/%Y").replace(tzinfo=_IST)
                dt_utc = dt_local.astimezone(timezone.utc)
            except ValueError:
                quality_flags.append(f"unparseable_timestamp:{raw_ts}")
                dt_utc = datetime.now(timezone.utc)

        # 2. Parse cumulative kWh
        kwh: float = 0.0
        try:
            kwh = float(item["kwh"])
            if prev_kwh is not None and kwh < prev_kwh:
                quality_flags.append("non_monotonic_kwh")
            prev_kwh = kwh
        except (KeyError, ValueError, TypeError):
            quality_flags.append("unparseable_kwh")

        # 3. Parse cumulative kVAh
        kvah: float = 0.0
        try:
            kvah = float(item["kvah"])
        except (KeyError, ValueError, TypeError):
            quality_flags.append("unparseable_kvah")

        # 4. Parse voltage
        voltage_r: float | None = None
        volt_raw = item.get("voltR")
        if volt_raw is not None and str(volt_raw).strip() not in ("", "—", "-", "null", "none"):
            try:
                voltage_r = float(volt_raw)
            except (ValueError, TypeError):
                quality_flags.append("unparseable_voltage")

        reading = Reading(
            meter_id=meter_id,
            raw_timestamp=raw_ts,
            timestamp=dt_utc,
            kwh=kwh,
            kvah=kvah,
            voltage_r=voltage_r,
            quality_flags=quality_flags,
        )
        readings.append(reading)

    return readings
