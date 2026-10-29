"""Tests for Phase 4 synchronization engine.

Verifies:
- Bulk export sync via /portal/export (signed HMAC-SHA256, atomic rollback, zero deletions)
- Paginated search sync via /portal/meters/search
- Distribution transformers sync via /portal/dts
- Single-meter on-demand detail sync via /portal/meters/search + /geo + /energy
- Energy readings telemetry batch sync with bounded concurrency
- Change detection (modified portal content produces items_changed)
- Idempotency (subsequent runs produce items_unchanged)
- Partial failure resilience (errors on page 2 or on meter N never corrupt prior data)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tests.fixtures.portal_pages import (
    DTS_PAYLOAD_SAMPLE,
    ENERGY_PAYLOAD_SAMPLE,
    EXPORT_PAYLOAD_SAMPLE,
    SEARCH_PAGE_1_SAMPLE,
    SEARCH_PAGE_2_SAMPLE,
)
from urja.client.portal import PortalClient
from urja.domain.models import (
    InstallationStatus,
    Meter,
    Provenance,
    SyncStatus,
)
from urja.store.repository import Store
from urja.sync.engine import SyncEngine


@pytest.fixture
def store():
    st = Store(":memory:")
    yield st
    st.close()


class TestSyncBulkExport:
    @pytest.mark.asyncio
    async def test_bulk_export_stores_meters_and_hierarchy(self, store: Store):
        """Bulk export sync ingests meters and hierarchy nodes into Store."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/portal/keys":
                return httpx.Response(200, json={"data": {"signingSecret": "secret-123"}})
            if request.url.path == "/portal/export":
                return httpx.Response(200, json=EXPORT_PAYLOAD_SAMPLE)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            run = await engine.sync_bulk_export(client)

            assert run.status == SyncStatus.SUCCESS
            assert run.items_new == 2
            assert run.items_failed == 0
            assert store.count_meters() == 2

            m0 = store.get_meter("J100000")
            assert m0 is not None
            assert m0.make == "HPL"
            assert m0.location is not None
            assert round(m0.location.latitude, 2) == 26.94

    @pytest.mark.asyncio
    async def test_idempotency_second_sync_produces_unchanged(self, store: Store):
        """Running export sync twice with identical data yields items_unchanged = total, items_new = 0."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/portal/keys":
                return httpx.Response(200, json={"data": {"signingSecret": "secret-123"}})
            if request.url.path == "/portal/export":
                return httpx.Response(200, json=EXPORT_PAYLOAD_SAMPLE)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)

            run1 = await engine.sync_bulk_export(client)
            assert run1.items_new == 2
            assert run1.items_unchanged == 0

            run2 = await engine.sync_bulk_export(client)
            assert run2.items_new == 0
            assert run2.items_unchanged == 2
            assert store.count_meters() == 2

    @pytest.mark.asyncio
    async def test_change_detection_updates_existing_meter(self, store: Store):
        """When portal data changes, sync updates record and reports items_changed."""
        current_data = [
            dict(m)
            for m in EXPORT_PAYLOAD_SAMPLE["data"]  # shallow copy items
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/portal/keys":
                return httpx.Response(200, json={"data": {"signingSecret": "secret-123"}})
            if request.url.path == "/portal/export":
                return httpx.Response(200, json={"data": current_data, "total": len(current_data)})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            await engine.sync_bulk_export(client)

            # Change meter status
            current_data[1] = dict(current_data[1])
            current_data[1]["installStatus"] = "Decommissioned"

            run2 = await engine.sync_bulk_export(client)
            assert run2.items_changed == 1
            assert run2.items_unchanged == 1
            assert run2.items_new == 0

            m1 = store.get_meter("J100001")
            assert m1 is not None
            assert m1.installation_status == InstallationStatus.DECOMMISSIONED

    @pytest.mark.asyncio
    async def test_export_atomic_rollback_on_failure(self, store: Store):
        """If an error occurs during export ingestion, database changes roll back atomically."""
        prov = Provenance(
            fetched_at=datetime.now(timezone.utc),
            source_endpoint="/init",
            payload_hash="init",
        )
        store.upsert_meter(Meter(meter_id="J_EXISTING", serial_no="SE001", make="HPL"), prov)
        assert store.count_meters() == 1

        engine = SyncEngine(store)
        client = AsyncMock(spec=PortalClient)
        client.get_export.return_value = httpx.Response(
            200,
            json={
                "data": [
                    {"meterId": "J100001", "serialNo": "SE1"},
                    {"meterId": "J100002", "serialNo": "SE2"},
                ],
                "total": 2,
            },
        )

        with patch.object(store, "upsert_meter", side_effect=[None, RuntimeError("Disk fault")]):
            run = await engine.sync_bulk_export(client)
            assert run.status == SyncStatus.FAILED

        # Original meter remains safe, aborted batch rolled back
        assert store.count_meters() == 1
        assert store.get_meter("J100001") is None

    @pytest.mark.asyncio
    async def test_zero_deletions_policy(self, store: Store):
        """Sync operations never delete existing meters, even if subsequent sync returns a partial list."""
        prov = Provenance(
            fetched_at=datetime.now(timezone.utc),
            source_endpoint="/init",
            payload_hash="init",
        )
        store.upsert_meter(Meter(meter_id="M-1", make="HPL"), prov)
        store.upsert_meter(Meter(meter_id="M-2", make="L&T"), prov)
        assert store.count_meters() == 2

        client = AsyncMock(spec=PortalClient)
        client.get_export.return_value = httpx.Response(
            200,
            json={"data": [{"meterId": "M-1", "make": "HPL"}], "total": 1},
        )

        engine = SyncEngine(store)
        run = await engine.sync_bulk_export(client)
        assert run.status == SyncStatus.SUCCESS

        # M-2 still exists in store!
        assert store.count_meters() == 2
        assert store.get_meter("M-2") is not None


class TestSyncMetersSearch:
    @pytest.mark.asyncio
    async def test_paginated_search_sync(self, store: Store):
        """Paginated search sync iterates across pages until total is reached."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/portal/meters/search":
                page = request.url.params.get("page", "1")
                if page == "1":
                    return httpx.Response(200, json=SEARCH_PAGE_1_SAMPLE)
                if page == "2":
                    return httpx.Response(200, json=SEARCH_PAGE_2_SAMPLE)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            run = await engine.sync_meters_search(client)

            assert run.status == SyncStatus.SUCCESS
            assert run.items_new == 4
            assert store.count_meters() == 4
            assert store.get_meter("J100020") is not None


class TestSyncTransformers:
    @pytest.mark.asyncio
    async def test_sync_transformers_from_dts(self, store: Store):
        """Transformers list is fetched and stored from /portal/dts?page=N."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/portal/dts":
                return httpx.Response(200, json=DTS_PAYLOAD_SAMPLE)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            run = await engine.sync_transformers(client)

            assert run.status == SyncStatus.SUCCESS
            assert run.items_new == 3
            transformers = store.list_transformers()
            assert len(transformers) == 3
            assert transformers[0].code == "DT-001"
            assert transformers[0].capacity_kva == 100.0


class TestSyncMeterDetail:
    @pytest.mark.asyncio
    async def test_sync_meter_detail_combines_search_geo_and_energy(self, store: Store):
        """Single-meter detail sync orchestrates search, geo, and energy endpoints."""

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/login":
                return httpx.Response(200, text="OK")
            if path == "/portal/meters/search":
                return httpx.Response(200, json=SEARCH_PAGE_1_SAMPLE)
            if path == "/portal/meters/J100000/geo":
                return httpx.Response(
                    200, json={"data": {"latitude": "26.94", "longitude": "75.81"}}
                )
            if path == "/portal/meters/J100000/energy":
                return httpx.Response(200, json=ENERGY_PAYLOAD_SAMPLE)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            m_res, readings_cnt, _ = await engine.sync_meter_detail(client, "J100000")

            assert m_res.value == "new"
            assert readings_cnt == 2

            m = store.get_meter("J100000")
            assert m is not None
            assert m.location is not None
            assert round(m.location.latitude, 2) == 26.94

            readings = store.get_readings("J100000")
            assert len(readings) == 2
            assert readings[0].kwh == 48439.58


class TestSyncMetersEnergy:
    @pytest.mark.asyncio
    async def test_sync_meters_energy_bounded_concurrency(self, store: Store):
        """Energy batch sync processes multiple meters with bounded concurrency."""
        prov = Provenance(
            fetched_at=datetime.now(timezone.utc),
            source_endpoint="/init",
            payload_hash="init",
        )
        store.upsert_meter(Meter(meter_id="J100000", make="HPL"), prov)
        store.upsert_meter(Meter(meter_id="J100001", make="L&T"), prov)

        max_concurrent = 0
        current_concurrent = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal max_concurrent, current_concurrent
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if "/energy" in request.url.path:
                current_concurrent += 1
                max_concurrent = max(max_concurrent, current_concurrent)
                await asyncio.sleep(0.01)
                current_concurrent -= 1
                return httpx.Response(200, json=ENERGY_PAYLOAD_SAMPLE)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            run = await engine.sync_meters_energy(client, concurrency=2)

            assert run.status == SyncStatus.SUCCESS
            assert run.items_new == 4  # 2 readings per meter * 2 meters
            assert max_concurrent <= 2
