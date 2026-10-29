"""Adapter for the portal transformers list page (``/transformers``).

Parses the single un-paginated HTML table into a list of
:class:`~urja.domain.models.Transformer` objects.

Portal table columns (observed): CODE, NAME, FEEDER, CAPACITY (KVA)

Known quirks handled
--------------------
- Column header is ``CAPACITY (KVA)`` — contains parentheses; matched
  by stripping to ``capacity (kva)`` and using a flexible header check.
- All DTs are on one page; no pagination.
- No individual detail page exists (``/transformers/DT-001`` → 404).
- Capacity values observed: 63, 100, 160, 250, 400 (always integers
  in practice, stored as float for future-proofing).
- An unparseable capacity gets ``capacity_unparseable`` quality flag
  rather than raising.
"""

from __future__ import annotations

import logging

from urja.adapters.base import (
    collapse_whitespace,
    make_soup,
    parse_float_flag,
    parse_table_rows,
)
from urja.client.exceptions import PortalShapeError
from urja.domain.models import Transformer

logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = ["code", "name", "feeder", "capacity (kva)"]

_NULL_SENTINELS = {"", "—", "-", "n/a", "null", "none"}


def parse_transformers(html: str) -> list[Transformer]:
    """Parse the transformers list page HTML.

    Args:
        html: Raw HTML from ``GET /transformers``.

    Returns:
        List of :class:`Transformer` objects.

    Raises:
        :exc:`~urja.client.exceptions.PortalShapeError`: If the expected
            table or required columns are absent.
    """
    soup = make_soup(html)

    table = soup.find("table")
    if table is None:
        raise PortalShapeError(
            "Transformers page has no <table> element. Portal may have changed.",
            field="table",
        )

    rows = parse_table_rows(table, _REQUIRED_COLUMNS)
    if not rows:
        logger.debug("Transformers table has zero data rows.")
        return []

    transformers: list[Transformer] = []
    for row in rows:
        flags: list[str] = []

        code = row.get("code", "").strip().upper()
        if not code or code.lower() in _NULL_SENTINELS:
            logger.warning("Skipping transformer row with empty code: %s", row)
            continue

        name = collapse_whitespace(row.get("name", ""))
        if not name:
            flags.append("missing_name")

        feeder = row.get("feeder", "").strip()
        if not feeder or feeder.lower() in _NULL_SENTINELS:
            flags.append("missing_feeder")
            feeder = ""

        capacity_raw = row.get("capacity (kva)", "").strip()
        capacity = parse_float_flag(capacity_raw, "capacity", flags)
        if capacity is None:
            capacity = 0.0  # store sentinel; flag already added

        transformers.append(
            Transformer(
                code=code,
                name=name,
                feeder=feeder,
                capacity_kva=capacity,
                quality_flags=flags,
            )
        )

    return transformers
