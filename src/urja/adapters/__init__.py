# Adapters — parse portal responses into domain shapes (the ONLY layer that knows portal formats).

from __future__ import annotations

from urja.adapters.meter_detail import MeterDetail, parse_meter_detail
from urja.adapters.meters_list import parse_meters_list, parse_pagination, parse_total_count
from urja.adapters.transformers import parse_transformers

__all__ = [
    "MeterDetail",
    "parse_meter_detail",
    "parse_meters_list",
    "parse_pagination",
    "parse_total_count",
    "parse_transformers",
]
