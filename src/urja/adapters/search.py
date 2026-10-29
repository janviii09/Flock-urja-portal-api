"""Adapter for the paginated /portal/meters/search JSON endpoint."""

from __future__ import annotations

import logging
from typing import Any

from urja.client.exceptions import PortalShapeError
from urja.domain.models import InstallationStatus, Meter, PhaseType

logger = logging.getLogger(__name__)


def parse_search_payload(
    payload: dict[str, Any],
) -> tuple[list[Meter], int, int, int]:
    """Parse JSON response from GET /portal/meters/search?q=&page=N.

    Args:
        payload: Decoded JSON dictionary.

    Returns:
        tuple of (meters, total_count, current_page, page_size)

    Raises:
        PortalShapeError: If payload lacks required structure.
    """
    if (
        not isinstance(payload, dict)
        or "data" not in payload
        or not isinstance(payload["data"], list)
    ):
        raise PortalShapeError(
            "Search payload missing 'data' array.",
            field="data",
        )

    raw_meters = payload["data"]
    total = int(payload.get("total", len(raw_meters)))
    page = int(payload.get("page", 1))
    page_size = int(payload.get("pageSize", 20))

    meters: list[Meter] = []

    for item in raw_meters:
        if not isinstance(item, dict):
            continue

        meter_id = str(item.get("meterId", "")).strip().upper()
        if not meter_id:
            continue

        quality_flags: list[str] = []

        # Phase type
        phase_raw = item.get("phaseType")
        phase_type: PhaseType | None = None
        if phase_raw:
            try:
                phase_type = PhaseType(str(phase_raw).strip().lower())
            except ValueError:
                quality_flags.append(f"unknown_phase:{phase_raw}")

        # Status
        status_raw = item.get("installStatus")
        install_status: InstallationStatus | None = None
        if status_raw:
            try:
                install_status = InstallationStatus(str(status_raw).strip().title())
            except ValueError:
                quality_flags.append(f"unknown_status:{status_raw}")

        serial_no = str(item["serialNo"]).strip() if item.get("serialNo") else None
        make = str(item["make"]).strip() if item.get("make") else None
        dt_code = str(item["dtCode"]).strip().upper() if item.get("dtCode") else None

        meter = Meter(
            meter_id=meter_id,
            serial_no=serial_no,
            make=make,
            phase_type=phase_type,
            installation_status=install_status,
            dt_code=dt_code,
            quality_flags=quality_flags,
        )
        meters.append(meter)

    return meters, total, page, page_size
