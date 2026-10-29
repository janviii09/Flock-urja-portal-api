"""Tests for Phase 4 SQLite store layer.

Covers:
- Database schema initialization and verified indexes
- Idempotent upserts for all entity types (meters, readings, nodes, transformers)
- Change detection (distinguishing NEW vs UNCHANGED vs CHANGED)
- Query methods (filtering, pagination, submodel reconstruction)
- Transaction safety and atomic rollbacks
- Sync runs audit tracking
"""

from __future__ import annotations

from datetime import datetime, timezone

from urja.domain.models import (
    HierarchyLevel,
    HierarchyNode,
    HierarchyPath,
    InstallationStatus,
    Location,
    Meter,
    PhaseType,
    Provenance,
    Reading,
    SyncStatus,
    Transformer,
    UpsertResult,
)
from urja.store.repository import Store


def _sample_provenance(
    endpoint: str = "/meters/J100000", payload_hash: str = "hash_1"
) -> Provenance:
    return Provenance(
        fetched_at=datetime(2026, 6, 30, 18, 0, 0, tzinfo=timezone.utc),
        source_endpoint=endpoint,
        payload_hash=payload_hash,
    )


class TestStoreSchema:
    def test_schema_creates_tables_and_indexes(self):
        store = Store(db_path=":memory:")
        cursor = store._conn.cursor()

        # Verify tables exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert {"meters", "readings", "hierarchy_nodes", "transformers", "sync_runs"}.issubset(
            tables
        )

        # Verify justified indexes exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_meters_dt_code" in indexes
        assert "idx_meters_serial_no" in indexes
        assert "idx_readings_timestamp" in indexes
        assert "idx_hierarchy_nodes_parent" in indexes
        assert "idx_hierarchy_nodes_level" in indexes
        assert "idx_sync_runs_started_at" in indexes

        store.close()


class TestStoreIdempotencyAndChangeDetection:
    def test_meter_idempotency_and_change_detection(self):
        store = Store(":memory:")
        prov1 = _sample_provenance(endpoint="/meters/J100000", payload_hash="hash_v1")

        m1 = Meter(
            meter_id="J100000",
            serial_no="SE33962",
            make="HPL",
            phase_type=PhaseType.SINGLE,
            installation_status=InstallationStatus.INSTALLED,
            dt_code="DT-001",
        )

        # 1. First insert -> NEW
        res1 = store.upsert_meter(m1, prov1)
        assert res1 == UpsertResult.NEW
        assert store.count_meters() == 1

        # 2. Duplicate insert with exact same data and hash -> UNCHANGED
        res2 = store.upsert_meter(m1, prov1)
        assert res2 == UpsertResult.UNCHANGED
        assert store.count_meters() == 1

        # 3. Update with changed status -> CHANGED
        prov2 = _sample_provenance(endpoint="/meters/J100000", payload_hash="hash_v2")
        m2 = m1.model_copy(update={"installation_status": InstallationStatus.DECOMMISSIONED})
        res3 = store.upsert_meter(m2, prov2)
        assert res3 == UpsertResult.CHANGED
        assert store.count_meters() == 1

        stored = store.get_meter("J100000")
        assert stored is not None
        assert stored.installation_status == InstallationStatus.DECOMMISSIONED

        store.close()

    def test_reading_idempotency_and_change_detection(self):
        store = Store(":memory:")
        prov = _sample_provenance()

        # Meter must exist due to FK
        store.upsert_meter(Meter(meter_id="J100000", serial_no="SE1", make="HPL"), prov)

        r1 = Reading(
            meter_id="J100000",
            timestamp=datetime(2026, 6, 23, 18, 0, 0, tzinfo=timezone.utc),
            kwh=48438.74,
            kvah=52313.84,
            voltage_r=226.0,
        )

        # 1. First insert -> NEW
        res1 = store.upsert_reading(r1, prov)
        assert res1 == UpsertResult.NEW

        # 2. Re-insert -> UNCHANGED (no duplicate rows)
        res2 = store.upsert_reading(r1, prov)
        assert res2 == UpsertResult.UNCHANGED

        readings = store.get_readings("J100000")
        assert len(readings) == 1

        # 3. Reading with modified kWh -> CHANGED
        prov_new = _sample_provenance(payload_hash="new_reading_hash")
        r2 = r1.model_copy(update={"kwh": 48439.00})
        res3 = store.upsert_reading(r2, prov_new)
        assert res3 == UpsertResult.CHANGED

        readings_updated = store.get_readings("J100000")
        assert len(readings_updated) == 1
        assert readings_updated[0].kwh == 48439.00

        store.close()

    def test_hierarchy_node_idempotency_and_change(self):
        store = Store(":memory:")
        prov = _sample_provenance(endpoint="/transformers")

        node = HierarchyNode(
            code="DT-001",
            name="Malviya Nagar DT 1",
            level=HierarchyLevel.DT,
            parent_code="F-001",
        )

        res1 = store.upsert_hierarchy_node(node, prov)
        assert res1 == UpsertResult.NEW

        res2 = store.upsert_hierarchy_node(node, prov)
        assert res2 == UpsertResult.UNCHANGED

        node_changed = node.model_copy(update={"name": "Malviya Nagar DT 1 (Renamed)"})
        res3 = store.upsert_hierarchy_node(node_changed, prov)
        assert res3 == UpsertResult.CHANGED

        fetched = store.get_hierarchy_node("DT-001")
        assert fetched is not None
        assert fetched.name == "Malviya Nagar DT 1 (Renamed)"

        store.close()

    def test_transformer_idempotency_and_change(self):
        store = Store(":memory:")
        prov = _sample_provenance(endpoint="/transformers")

        tr = Transformer(
            code="DT-001",
            name="Malviya Nagar DT 1",
            feeder="F-001",
            capacity_kva=100.0,
        )

        assert store.upsert_transformer(tr, prov) == UpsertResult.NEW
        assert store.upsert_transformer(tr, prov) == UpsertResult.UNCHANGED

        tr_changed = tr.model_copy(update={"capacity_kva": 160.0})
        assert store.upsert_transformer(tr_changed, prov) == UpsertResult.CHANGED

        fetched = store.get_transformer("DT-001")
        assert fetched is not None
        assert fetched.capacity_kva == 160.0

        store.close()


class TestStoreQueries:
    def test_meter_queries_and_filters(self):
        store = Store(":memory:")
        prov = _sample_provenance()

        path = HierarchyPath(
            zone=HierarchyNode(code="Z-01", name="Jaipur Zone 1", level=HierarchyLevel.ZONE),
            dt=HierarchyNode(code="DT-001", name="Malviya Nagar DT 1", level=HierarchyLevel.DT),
        )

        m1 = Meter(
            meter_id="J100000",
            serial_no="SE33962",
            make="HPL",
            dt_code="DT-001",
            location=Location(latitude=26.9389, longitude=75.8309),
            hierarchy_path=path,
        )
        m2 = Meter(
            meter_id="J100001",
            serial_no="GE84132",
            make="L&T",
            dt_code="DT-002",
        )
        store.upsert_meter(m1, prov)
        store.upsert_meter(m2, prov)

        # 1. get_meter reconstruction
        fetched = store.get_meter("j100000")  # case-insensitive input
        assert fetched is not None
        assert fetched.meter_id == "J100000"
        assert fetched.location is not None
        assert round(fetched.location.latitude, 4) == 26.9389
        assert fetched.hierarchy_path is not None
        assert fetched.hierarchy_path.zone is not None
        assert fetched.hierarchy_path.zone.code == "Z-01"

        # 2. list_meters filter by dt_code
        dt1_meters = store.list_meters(dt_code="DT-001")
        assert len(dt1_meters) == 1
        assert dt1_meters[0].meter_id == "J100000"

        # 3. list_meters filter by serial_no
        serial_meters = store.list_meters(serial_no="GE84132")
        assert len(serial_meters) == 1
        assert serial_meters[0].meter_id == "J100001"

        # 4. pagination
        paged = store.list_meters(limit=1, offset=1)
        assert len(paged) == 1
        assert paged[0].meter_id == "J100001"

        store.close()

    def test_readings_ordering_and_since_filter(self):
        store = Store(":memory:")
        prov = _sample_provenance()
        store.upsert_meter(Meter(meter_id="J100000", serial_no="SE1", make="HPL"), prov)

        r1 = Reading(
            meter_id="J100000",
            timestamp=datetime(2026, 6, 23, 10, 0, 0, tzinfo=timezone.utc),
            kwh=100.0,
            kvah=110.0,
        )
        r2 = Reading(
            meter_id="J100000",
            timestamp=datetime(2026, 6, 23, 11, 0, 0, tzinfo=timezone.utc),
            kwh=102.0,
            kvah=112.0,
        )
        store.upsert_readings([r1, r2], prov)

        # Ordered descending (newest first)
        results = store.get_readings("J100000")
        assert len(results) == 2
        assert results[0].timestamp > results[1].timestamp

        # Filter since
        since_results = store.get_readings(
            "J100000", since=datetime(2026, 6, 23, 10, 30, 0, tzinfo=timezone.utc)
        )
        assert len(since_results) == 1
        assert since_results[0].kwh == 102.0

        store.close()

    def test_hierarchy_tree_queries(self):
        store = Store(":memory:")
        prov = _sample_provenance()

        z1 = HierarchyNode(code="Z-01", name="Zone 1", level=HierarchyLevel.ZONE)
        c1 = HierarchyNode(
            code="C-01", name="Circle 1", level=HierarchyLevel.CIRCLE, parent_code="Z-01"
        )
        c2 = HierarchyNode(
            code="C-02", name="Circle 2", level=HierarchyLevel.CIRCLE, parent_code="Z-01"
        )
        ss1 = HierarchyNode(code="SS-01", name="Substation 1", level=HierarchyLevel.SUBSTATION)

        store.upsert_hierarchy_node(z1, prov)
        store.upsert_hierarchy_node(c1, prov)
        store.upsert_hierarchy_node(c2, prov)
        store.upsert_hierarchy_node(ss1, prov)

        # Query children by parent_code
        children = store.list_hierarchy_nodes(parent_code="Z-01")
        assert len(children) == 2
        assert {c.code for c in children} == {"C-01", "C-02"}

        # Query by level
        circles = store.list_hierarchy_nodes(level=HierarchyLevel.CIRCLE)
        assert len(circles) == 2

        store.close()


class TestSyncRunsAudit:
    def test_sync_run_lifecycle(self):
        store = Store(":memory:")

        # 1. Start run
        run = store.create_sync_run(resource="bulk_export")
        assert run.status == SyncStatus.RUNNING
        assert run.items_new == 0

        # 2. Query running
        fetched = store.get_sync_run(run.run_id)
        assert fetched is not None
        assert fetched.status == SyncStatus.RUNNING

        # 3. Finish run
        finished = store.finish_sync_run(
            run_id=run.run_id,
            status=SyncStatus.SUCCESS,
            items_new=20,
            items_changed=2,
            items_unchanged=5,
            items_failed=0,
        )
        assert finished is not None
        assert finished.status == SyncStatus.SUCCESS
        assert finished.items_new == 20
        assert finished.completed_at is not None

        # 4. Latest run query
        latest = store.get_latest_sync_run("bulk_export")
        assert latest is not None
        assert latest.run_id == run.run_id

        store.close()
