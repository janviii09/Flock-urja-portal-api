"""Unit tests for Urja portal adapters.

Tests the 4 verified JSON adapters:
1. Export adapter (GET /portal/export)
2. Search adapter (GET /portal/meters/search)
3. Energy telemetry adapter (GET /portal/meters/{id}/energy)
4. Distribution transformers adapter (GET /portal/dts)
"""

from __future__ import annotations

import pytest

from urja.adapters import (
    parse_dts_payload,
    parse_energy_payload,
    parse_export_payload,
    parse_search_payload,
)
from urja.client.exceptions import PortalShapeError
from urja.domain.models import (
    HierarchyLevel,
    InstallationStatus,
    PhaseType,
)
from urja.hierarchy.tree import HierarchyGraph


class TestExportAdapter:
    def test_export_normal_payload(self):
        """Standard export payload parses into domain Meters and HierarchyNodes."""
        payload = {
            "data": [
                {
                    "meterId": "J100000",
                    "serialNo": "SE33962",
                    "make": "HPL",
                    "phaseType": "single",
                    "installStatus": "Installed",
                    "installType": "Whole Current",
                    "build": "legacy",
                    "dtCode": "DT-001",
                    "hierarchy": {
                        "zone": {"name": "Jaipur Zone 1", "code": "Z-01"},
                        "circle": {"name": "Circle 1", "code": "C-01"},
                        "division": {"name": "Division 1", "code": "D-01"},
                        "subdivision": {"name": "Subdivision 1", "code": "SD-01"},
                        "substation": {"name": "Substation 1", "code": "S-01"},
                        "feeder": {"name": "Feeder 1", "code": "F-01"},
                        "dt": {"name": "DT 1", "code": "DT-001"},
                    },
                    "geo": {"lat": 26.93896, "lng": 75.83095},
                }
            ],
            "total": 1,
        }
        meters, nodes, total = parse_export_payload(payload)
        assert total == 1
        assert len(meters) == 1
        m = meters[0]
        assert m.meter_id == "J100000"
        assert m.serial_no == "SE33962"
        assert m.make == "HPL"
        assert m.phase_type == PhaseType.SINGLE
        assert m.installation_status == InstallationStatus.INSTALLED
        assert m.installation_type == "Whole Current"
        assert m.build == "legacy"
        assert m.dt_code == "DT-001"
        assert m.location is not None
        assert round(m.location.latitude, 4) == 26.9390
        assert round(m.location.longitude, 4) == 75.8310

        # Hierarchy nodes: 7 levels
        assert len(nodes) == 7
        levels = [n.level for n in nodes]
        assert levels == [
            HierarchyLevel.ZONE,
            HierarchyLevel.CIRCLE,
            HierarchyLevel.DIVISION,
            HierarchyLevel.SUBDIVISION,
            HierarchyLevel.SUBSTATION,
            HierarchyLevel.FEEDER,
            HierarchyLevel.DT,
        ]

    def test_export_missing_hierarchy_fields(self):
        """Meters with empty code or empty name at some hierarchy levels are safely assigned slug fallbacks."""
        payload = {
            "data": [
                {
                    "meterId": "J100011",
                    "serialNo": "SE10011",
                    "make": "HPL",
                    "phaseType": "single",
                    "installStatus": "Installed",
                    "installType": "Whole Current",
                    "build": "legacy",
                    "dtCode": "DT-001",
                    "hierarchy": {
                        "zone": {"name": "Jaipur Zone 1", "code": "Z-01"},
                        "circle": {"name": "Circle 1", "code": ""},  # Empty code
                        "division": {"name": "", "code": "D-01"},  # Empty name
                        "subdivision": {"name": "", "code": ""},  # Both empty
                        "substation": {"name": "Substation 1", "code": "S-01"},
                        "feeder": {"name": "Feeder 1", "code": "F-01"},
                        "dt": {"name": "DT 1", "code": "DT-001"},
                    },
                    "geo": {"lat": 26.9124, "lng": 75.7873},
                }
            ],
            "total": 1,
        }

        meters, nodes, _total = parse_export_payload(payload)
        assert len(meters) == 1
        m = meters[0]
        assert m.meter_id == "J100011"
        assert m.build == "legacy"
        assert any("missing_hierarchy_code:circle" in f for f in m.quality_flags)
        assert any("missing_hierarchy_name:division" in f for f in m.quality_flags)

        assert len(nodes) == 7
        circle_node = next(n for n in nodes if n.level == HierarchyLevel.CIRCLE)
        assert "missing_hierarchy_code" in circle_node.quality_flags
        assert circle_node.name == "Circle 1"
        assert circle_node.node_path.startswith("Z-01/")

    def test_duplicate_codes_different_parents(self):
        """Subdivision SD-01 appearing under D-01 and D-05 produces distinct node_paths."""
        payload = {
            "data": [
                {
                    "meterId": "J100001",
                    "hierarchy": {
                        "zone": {"name": "Zone 1", "code": "Z-01"},
                        "circle": {"name": "Circle 1", "code": "C-01"},
                        "division": {"name": "Division 1", "code": "D-01"},
                        "subdivision": {"name": "Subdivision 1", "code": "SD-01"},
                        "substation": {"name": "Substation 1", "code": "S-01"},
                        "feeder": {"name": "Feeder 1", "code": "F-01"},
                        "dt": {"name": "DT 1", "code": "DT-001"},
                    },
                },
                {
                    "meterId": "J100002",
                    "hierarchy": {
                        "zone": {"name": "Zone 1", "code": "Z-01"},
                        "circle": {"name": "Circle 2", "code": "C-02"},
                        "division": {"name": "Division 5", "code": "D-05"},
                        "subdivision": {"name": "Subdivision 1", "code": "SD-01"},
                        "substation": {"name": "Substation 2", "code": "S-02"},
                        "feeder": {"name": "Feeder 2", "code": "F-02"},
                        "dt": {"name": "DT 2", "code": "DT-002"},
                    },
                },
            ],
            "total": 2,
        }

        meters, nodes, _total = parse_export_payload(payload)
        sd_nodes = [n for n in nodes if n.level == HierarchyLevel.SUBDIVISION]
        assert len(sd_nodes) == 2

        paths = {n.node_path for n in sd_nodes}
        assert "Z-01/C-01/D-01/SD-01" in paths
        assert "Z-01/C-02/D-05/SD-01" in paths

        graph = HierarchyGraph(nodes, meters)
        tree, defects = graph.analyze_and_build()
        assert "SD-01" in defects["multiple_parents"]
        assert len(tree) >= 1

    def test_export_null_island_flagged(self):
        """Coordinates (0.0, 0.0) receive null_island_coordinates flag."""
        payload = {
            "data": [
                {
                    "meterId": "J100099",
                    "geo": {"lat": 0.0, "lng": 0.0},
                }
            ],
            "total": 1,
        }
        meters, _, _ = parse_export_payload(payload)
        assert "null_island_coordinates" in meters[0].location.quality_flags

    def test_export_shape_error_on_invalid_payload(self):
        """Invalid payload raises PortalShapeError."""
        with pytest.raises(PortalShapeError):
            parse_export_payload({"status": "ok"})


class TestSearchAdapter:
    def test_search_payload_parsing(self):
        """Search adapter parses paginated search JSON response."""
        payload = {
            "data": [
                {
                    "meterId": "J100000",
                    "serialNo": "SE33962",
                    "make": "HPL",
                    "phaseType": "single",
                    "installStatus": "Installed",
                    "dtCode": "DT-001",
                }
            ],
            "total": 403,
            "page": 1,
            "pageSize": 20,
        }

        meters, total, page, page_size = parse_search_payload(payload)
        assert len(meters) == 1
        assert total == 403
        assert page == 1
        assert page_size == 20
        assert meters[0].meter_id == "J100000"
        assert meters[0].make == "HPL"
        assert meters[0].phase_type == PhaseType.SINGLE
        assert meters[0].installation_status == InstallationStatus.INSTALLED

    def test_search_shape_error_on_invalid_payload(self):
        with pytest.raises(PortalShapeError):
            parse_search_payload({"error": "unauthorized"})


class TestEnergyAdapter:
    def test_energy_raw_timestamp_preserved(self):
        """Energy adapter preserves raw literal unzoned timestamp string and normalizes to UTC."""
        payload = {
            "data": [
                {
                    "timestamp": "23/06/2026 23:30",
                    "kwh": 1420.5,
                    "kvah": 1510.2,
                    "voltR": 238.4,
                }
            ]
        }

        readings = parse_energy_payload(payload, meter_id="J100020")
        assert len(readings) == 1
        r = readings[0]
        assert r.meter_id == "J100020"
        assert r.raw_timestamp == "23/06/2026 23:30"
        # Asia/Kolkata (IST = UTC+05:30): 23:30 IST -> 18:00 UTC
        assert r.timestamp.hour == 18
        assert r.timestamp.minute == 0
        assert r.kwh == 1420.5
        assert r.voltage_r == 238.4
        assert len(r.quality_flags) == 0

    def test_energy_non_monotonic_flagged(self):
        """Non-monotonic cumulative kWh registers are flagged in quality_flags."""
        payload = {
            "data": [
                {"timestamp": "23/06/2026 12:00", "kwh": 500.0, "kvah": 510.0},
                {
                    "timestamp": "23/06/2026 12:30",
                    "kwh": 490.0,
                    "kvah": 515.0,
                },  # Drop from 500 to 490!
            ]
        }

        readings = parse_energy_payload(payload, meter_id="J100020")
        assert len(readings) == 2
        assert "non_monotonic_kwh" in readings[1].quality_flags

    def test_energy_shape_error_on_invalid_payload(self):
        with pytest.raises(PortalShapeError):
            parse_energy_payload({"error": "not found"}, meter_id="J100020")


class TestDtsAdapter:
    def test_dts_payload_parsing(self):
        """DTS adapter parses paginated JSON transformers list."""
        payload = {
            "data": [
                {
                    "code": "DT-001",
                    "name": "Malviya Nagar DT 1",
                    "feederCode": "F-001",
                    "capacityKva": 100,
                }
            ],
            "total": 40,
            "page": 1,
            "pageSize": 20,
        }
        transformers, total, page, page_size = parse_dts_payload(payload)
        assert len(transformers) == 1
        assert total == 40
        assert page == 1
        assert page_size == 20
        assert transformers[0].code == "DT-001"
        assert transformers[0].name == "Malviya Nagar DT 1"
        assert transformers[0].feeder == "F-001"
        assert transformers[0].capacity_kva == 100.0

    def test_dts_shape_error_on_invalid_payload(self):
        with pytest.raises(PortalShapeError):
            parse_dts_payload({"error": "service unavailable"})
