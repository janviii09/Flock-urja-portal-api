"""Adapters layer — pure functions parsing verified portal JSON payloads into domain models.

All adapters parse verified JSON endpoints from the Urja legacy portal:
- GET /portal/export?page=1 -> parse_export_payload
- GET /portal/meters/search?q=&page=N -> parse_search_payload
- GET /portal/meters/{id}/energy -> parse_energy_payload
- GET /portal/dts?page=N -> parse_dts_payload
"""

from urja.adapters.dts import parse_dts_payload
from urja.adapters.energy import parse_energy_payload
from urja.adapters.export import parse_export_payload
from urja.adapters.search import parse_search_payload

__all__ = [
    "parse_dts_payload",
    "parse_energy_payload",
    "parse_export_payload",
    "parse_search_payload",
]
