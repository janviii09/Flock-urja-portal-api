"""Adapter for the verified paginated /portal/dts?page=N JSON endpoint.

Transforms distribution transformer JSON responses into domain Transformer models.
"""

from __future__ import annotations

from typing import Any

from urja.client.exceptions import PortalShapeError
from urja.domain.models import Transformer


def parse_dts_payload(
    payload: dict[str, Any],
) -> tuple[list[Transformer], int, int, int]:
    """Parse JSON response from GET /portal/dts?page=N.

    Args:
        payload: Decoded JSON dictionary.

    Returns:
        tuple of (transformers, total_count, page, page_size)

    Raises:
        PortalShapeError: If payload lacks required 'data' list.
    """
    if (
        not isinstance(payload, dict)
        or "data" not in payload
        or not isinstance(payload["data"], list)
    ):
        raise PortalShapeError(
            "DTS payload missing 'data' array.",
            field="data",
        )

    raw_items = payload["data"]
    total = int(payload.get("total", len(raw_items)))
    page = int(payload.get("page", 1))
    page_size = int(payload.get("pageSize", 20))

    transformers: list[Transformer] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        code = str(item.get("code", "")).strip().upper()
        if not code:
            continue

        name = str(item.get("name", "")).strip() or code
        feeder = str(item.get("feederCode", item.get("feeder", ""))).strip().upper()

        capacity_raw = item.get("capacityKva", item.get("capacity", 0.0))
        try:
            capacity_kva = float(capacity_raw)
        except (ValueError, TypeError):
            capacity_kva = 0.0

        transformers.append(
            Transformer(
                code=code,
                name=name,
                feeder=feeder,
                capacity_kva=capacity_kva,
            )
        )

    return transformers, total, page, page_size
