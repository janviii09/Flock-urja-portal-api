"""Adapter for the bulk /portal/export JSON endpoint.

Transforms verified export payloads into domain Meter and HierarchyNode objects.
Hardened against empty codes, empty names, and multi-parent disambiguation.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from urja.client.exceptions import PortalShapeError
from urja.domain.models import (
    HierarchyLevel,
    HierarchyNode,
    InstallationStatus,
    Location,
    Meter,
    PhaseType,
)

logger = logging.getLogger(__name__)

_HIERARCHY_LEVELS: list[HierarchyLevel] = [
    HierarchyLevel.ZONE,
    HierarchyLevel.CIRCLE,
    HierarchyLevel.DIVISION,
    HierarchyLevel.SUBDIVISION,
    HierarchyLevel.SUBSTATION,
    HierarchyLevel.FEEDER,
    HierarchyLevel.DT,
]


def _slugify(text: str) -> str:
    """Produce URL/path safe slug from string."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "unnamed"


def parse_export_payload(
    payload: dict[str, Any],
) -> tuple[list[Meter], list[HierarchyNode], int]:
    """Parse bulk /portal/export JSON response into domain models.

    Args:
        payload: Decoded JSON dictionary from GET /portal/export.

    Returns:
        tuple of (meters, unique_hierarchy_nodes, total_count)

    Raises:
        PortalShapeError: If payload lacks required 'data' list.
    """
    if (
        not isinstance(payload, dict)
        or "data" not in payload
        or not isinstance(payload["data"], list)
    ):
        raise PortalShapeError(
            "Export payload missing 'data' array.",
            field="data",
        )

    raw_meters = payload["data"]
    total = int(payload.get("total", len(raw_meters)))

    meters: list[Meter] = []
    nodes_by_path: dict[str, HierarchyNode] = {}

    for item in raw_meters:
        if not isinstance(item, dict):
            continue

        meter_id = str(item.get("meterId", "")).strip().upper()
        if not meter_id:
            continue

        quality_flags: list[str] = []

        # 1. Parse phase type
        phase_raw = item.get("phaseType")
        phase_type: PhaseType | None = None
        if phase_raw:
            try:
                phase_type = PhaseType(str(phase_raw).strip().lower())
            except ValueError:
                phase_type = None
                quality_flags.append(f"unknown_phase:{phase_raw}")

        # 2. Parse installation status
        status_raw = item.get("installStatus")
        install_status: InstallationStatus | None = None
        if status_raw:
            try:
                install_status = InstallationStatus(str(status_raw).strip().title())
            except ValueError:
                install_status = None
                quality_flags.append(f"unknown_status:{status_raw}")

        # 3. Parse location / coordinates
        location: Location | None = None
        geo = item.get("geo")
        if isinstance(geo, dict):
            lat_val = geo.get("lat")
            lng_val = geo.get("lng")
            if lat_val is not None and lng_val is not None:
                try:
                    lat = float(lat_val)
                    lng = float(lng_val)
                    loc_flags: list[str] = []
                    if lat == 0.0 and lng == 0.0:
                        loc_flags.append("null_island_coordinates")
                        quality_flags.append("null_island_coordinates")
                    location = Location(latitude=lat, longitude=lng, quality_flags=loc_flags)
                except (ValueError, TypeError):
                    quality_flags.append("invalid_coordinates")

        # 4. Parse hierarchy path and synthesize nodes with hardened full-path keys
        raw_hierarchy = item.get("hierarchy")
        breadcrumb_parts: list[str] = []
        current_path: str = ""

        if isinstance(raw_hierarchy, dict):
            for level in _HIERARCHY_LEVELS:
                lvl_str = level.value
                node_data = raw_hierarchy.get(lvl_str)
                if not isinstance(node_data, dict):
                    quality_flags.append(f"missing_hierarchy_level:{lvl_str}")
                    continue

                code = str(node_data.get("code", "")).strip()
                name = str(node_data.get("name", "")).strip()
                node_flags: list[str] = []

                if not code:
                    node_flags.append("missing_hierarchy_code")
                    quality_flags.append(f"missing_hierarchy_code:{lvl_str}")
                if not name:
                    node_flags.append("missing_hierarchy_name")
                    quality_flags.append(f"missing_hierarchy_name:{lvl_str}")

                # Deterministic canonical segment calculation
                if code:
                    segment = code.upper()
                elif name:
                    segment = _slugify(name)
                else:
                    h = hashlib.sha256(f"{current_path}/{meter_id}".encode()).hexdigest()[:8]
                    segment = f"{lvl_str.upper()}_{h}"

                parent_path = current_path if current_path else None
                node_path = f"{current_path}/{segment}" if current_path else segment
                current_path = node_path

                # Format human breadcrumb segment: Name (CODE)
                display_name = name or "Unnamed"
                display_code = code or segment
                breadcrumb_parts.append(f"{display_name} ({display_code})")

                # Track unique node by its full path
                if node_path not in nodes_by_path:
                    nodes_by_path[node_path] = HierarchyNode(
                        node_path=node_path,
                        code=code or None,
                        name=name or None,
                        level=level,
                        parent_path=parent_path,
                        quality_flags=node_flags,
                    )
                elif node_flags:
                    # Accumulate quality flags if previously missing
                    for flag in node_flags:
                        if flag not in nodes_by_path[node_path].quality_flags:
                            nodes_by_path[node_path].quality_flags.append(flag)

        serial_no = str(item["serialNo"]).strip() if item.get("serialNo") else None
        make = str(item["make"]).strip() if item.get("make") else None
        dt_code = str(item["dtCode"]).strip().upper() if item.get("dtCode") else None
        install_type = str(item["installType"]).strip() if item.get("installType") else None
        build = str(item["build"]).strip() if item.get("build") else None

        meter = Meter(
            meter_id=meter_id,
            serial_no=serial_no,
            make=make,
            phase_type=phase_type,
            installation_status=install_status,
            installation_type=install_type,
            build=build,
            dt_code=dt_code,
            location=location,
            hierarchy_path=breadcrumb_parts if breadcrumb_parts else None,
            raw_hierarchy=raw_hierarchy if isinstance(raw_hierarchy, dict) else None,
            quality_flags=quality_flags,
        )
        meters.append(meter)

    return meters, list(nodes_by_path.values()), total
