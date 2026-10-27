"""Phase 1 tests — domain models, settings, and API health check."""

from datetime import datetime

from fastapi.testclient import TestClient

from urja.api.app import app
from urja.domain.models import (
    HierarchyLevel,
    HierarchyNode,
    HierarchyPath,
    InstallationStatus,
    Location,
    Meter,
    PhaseType,
    Reading,
    Transformer,
)
from urja.settings import Settings

# ── Settings ──────────────────────────────────────────────────────────


class TestSettings:
    def test_defaults(self):
        """Settings should have sane defaults even without a .env file."""
        s = Settings(
            urja_base_url="https://example.com",
            urja_email="test@test.com",
            urja_password="secret",
            _env_file=None,
        )
        assert s.urja_base_url == "https://example.com"
        assert s.log_level == "INFO"
        assert s.default_ttl == 300

    def test_credentials_from_env(self, monkeypatch):
        """Credentials must come from environment, never hardcoded."""
        monkeypatch.setenv("URJA_EMAIL", "a@b.com")
        monkeypatch.setenv("URJA_PASSWORD", "pass123")
        s = Settings(_env_file=None)
        assert s.urja_email == "a@b.com"
        assert s.urja_password == "pass123"


# ── Domain models ─────────────────────────────────────────────────────


class TestMeterModel:
    def test_create_minimal(self):
        """Meter can be created with just the list-page fields."""
        m = Meter(meter_id="J100000", serial_no="SE33962", make="HPL")
        assert m.meter_id == "J100000"
        assert m.quality_flags == []

    def test_create_full(self):
        """Meter can hold all detail-page fields."""
        m = Meter(
            meter_id="J100000",
            serial_no="SE33962",
            make="HPL",
            phase_type=PhaseType.SINGLE,
            installation_status=InstallationStatus.DECOMMISSIONED,
            installation_type="Whole Current",
            dt_code="DT-001",
            location=Location(latitude=26.938961, longitude=75.830957),
            quality_flags=["location_low_precision"],
        )
        assert m.phase_type == PhaseType.SINGLE
        assert m.installation_status == InstallationStatus.DECOMMISSIONED
        assert m.location is not None
        assert m.location.latitude == 26.938961
        assert "location_low_precision" in m.quality_flags

    def test_quality_flags_propagation(self):
        """Quality flags should be independent per sub-model."""
        loc = Location(
            latitude=0.0,
            longitude=0.0,
            quality_flags=["null_island"],
        )
        m = Meter(
            meter_id="J999",
            serial_no="XX0000",
            make="Unknown",
            location=loc,
            quality_flags=["unknown_make"],
        )
        assert m.quality_flags == ["unknown_make"]
        assert m.location.quality_flags == ["null_island"]


class TestReadingModel:
    def test_create(self):
        """Reading holds consumption data from the portal table."""
        r = Reading(
            meter_id="J100000",
            timestamp=datetime(2026, 6, 23, 23, 30),
            kwh=48438.74,
            kvah=52313.84,
            voltage_r=226,
        )
        assert r.kwh == 48438.74
        assert r.voltage_r == 226

    def test_missing_voltage(self):
        """Voltage can be None if the portal omits it."""
        r = Reading(
            meter_id="J100000",
            timestamp=datetime(2026, 6, 23, 23, 30),
            kwh=48438.74,
            kvah=52313.84,
        )
        assert r.voltage_r is None


class TestTransformerModel:
    def test_create(self):
        t = Transformer(
            code="DT-001",
            name="Malviya Nagar DT 1",
            feeder="F-001",
            capacity_kva=100,
        )
        assert t.code == "DT-001"
        assert t.capacity_kva == 100


class TestHierarchyModels:
    def test_node(self):
        node = HierarchyNode(
            code="Z-01",
            name="Jaipur Zone 1",
            level=HierarchyLevel.ZONE,
        )
        assert node.level == HierarchyLevel.ZONE

    def test_path(self):
        path = HierarchyPath(
            zone=HierarchyNode(code="Z-01", name="Jaipur Zone 1", level=HierarchyLevel.ZONE),
            dt=HierarchyNode(code="DT-001", name="Malviya Nagar DT 1", level=HierarchyLevel.DT),
        )
        assert path.zone.code == "Z-01"
        assert path.dt.name == "Malviya Nagar DT 1"
        assert path.circle is None  # sparse paths are fine


# ── API health check ──────────────────────────────────────────────────


class TestHealthEndpoint:
    def test_health(self):
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
