"""Tests for Phase 4 synchronization engine.

Verifies:
- Pagination over /meters and audit logging
- Idempotency (subsequent runs produce items_unchanged)
- Change detection (modified portal content produces items_changed)
- Full detail sync (ingesting nameplate, 7-level hierarchy, consumption readings)
- Partial failure resilience (errors on page 2 or on meter N never corrupt prior data)
"""

from __future__ import annotations

import httpx
import pytest

from tests.fixtures.portal_pages import (
    METER_DETAIL_HTML,
    METERS_LIST_PAGE_HTML,
    TRANSFORMERS_LIST_HTML,
)
from urja.client.portal import PortalClient
from urja.domain.models import InstallationStatus, SyncStatus
from urja.store.repository import Store
from urja.sync.engine import SyncEngine

PAGE_2_HTML = """\
<table>
  <thead>
    <tr>
      <th>METER</th><th>SERIAL</th><th>MAKE</th>
      <th>PHASE</th><th>STATUS</th><th>DT</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><a href="/meters/J100020">J100020</a></td>
      <td>SE99999</td><td>Secure</td><td>three</td>
      <td>Installed</td><td>DT-004</td>
    </tr>
  </tbody>
</table>
<div class="pagination">
  <button>Previous</button>
  <span>Page 2 of 2</span>
  <button disabled>Next</button>
</div>
"""


@pytest.fixture
def store():
    st = Store(":memory:")
    yield st
    st.close()


class TestSyncMetersList:
    @pytest.mark.asyncio
    async def test_paginated_sync_records_audit_and_stores_meters(self, store: Store):
        """Sync engine paginates through pages and records audit counts."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/meters":
                page = request.url.params.get("page", "1")
                if page == "1":
                    # returns page 1 of 2
                    p1_html = METERS_LIST_PAGE_HTML.replace("Page 1 of 21", "Page 1 of 2")
                    return httpx.Response(200, text=p1_html)
                if page == "2":
                    return httpx.Response(200, text=PAGE_2_HTML)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            run = await engine.sync_meters_list(client)

            assert run.status == SyncStatus.SUCCESS
            assert run.items_new == 4  # 3 from page 1 + 1 from page 2
            assert run.items_failed == 0
            assert store.count_meters() == 4

            # Verify meters stored properly
            m0 = store.get_meter("J100000")
            assert m0 is not None
            assert m0.make == "HPL"

            m20 = store.get_meter("J100020")
            assert m20 is not None
            assert m20.make == "Secure"

    @pytest.mark.asyncio
    async def test_idempotency_second_sync_produces_unchanged(self, store: Store):
        """Running sync twice with identical data yields items_unchanged = total, items_new = 0."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/meters":
                p1_single = METERS_LIST_PAGE_HTML.replace("Page 1 of 21", "Page 1 of 1")
                return httpx.Response(200, text=p1_single)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)

            # 1. First run: all new
            run1 = await engine.sync_meters_list(client, max_pages=1)
            assert run1.items_new == 3
            assert run1.items_unchanged == 0
            assert store.count_meters() == 3

            # 2. Second run: all unchanged
            run2 = await engine.sync_meters_list(client, max_pages=1)
            assert run2.items_new == 0
            assert run2.items_unchanged == 3
            assert store.count_meters() == 3

    @pytest.mark.asyncio
    async def test_change_detection_updates_existing_meter(self, store: Store):
        """When portal data changes, sync updates record and reports items_changed."""
        current_html = METERS_LIST_PAGE_HTML.replace("Page 1 of 21", "Page 1 of 1")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/meters":
                return httpx.Response(200, text=current_html)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            await engine.sync_meters_list(client, max_pages=1)

            # Now portal updates: J100001 status becomes Decommissioned
            current_html = current_html.replace(
                "<td>GE84132</td><td>L&amp;T</td><td>single</td>\n      <td>Installed</td>",
                "<td>GE84132</td><td>L&amp;T</td><td>single</td>\n      <td>Decommissioned</td>",
            )

            run2 = await engine.sync_meters_list(client, max_pages=1)
            assert run2.items_changed == 1
            assert run2.items_unchanged == 2
            assert run2.items_new == 0

            # Verify in store
            m1 = store.get_meter("J100001")
            assert m1 is not None
            assert m1.installation_status == InstallationStatus.DECOMMISSIONED


class TestSyncMeterDetail:
    @pytest.mark.asyncio
    async def test_sync_meter_detail_ingests_all_submodels(self, store: Store):
        """Detail sync ingests meter coordinates, 7-level breadcrumbs, and consumption readings."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/meters/J100000":
                return httpx.Response(200, text=METER_DETAIL_HTML)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            m_res, readings_cnt, nodes_cnt = await engine.sync_meter_detail(client, "J100000")

            assert m_res.value == "new"
            assert readings_cnt == 3
            assert nodes_cnt == 7

            # 1. Meter with location and hierarchy
            m = store.get_meter("J100000")
            assert m is not None
            assert m.location is not None
            assert round(m.location.latitude, 2) == 26.94
            assert m.hierarchy_path is not None
            assert m.hierarchy_path.dt is not None
            assert m.hierarchy_path.dt.code == "DT-001"

            # 2. Hierarchy nodes table populated
            z = store.get_hierarchy_node("Z-01")
            assert z is not None
            assert z.name == "Jaipur Zone 1"

            dt = store.get_hierarchy_node("DT-001")
            assert dt is not None
            assert dt.parent_code == "F-001"

            # 3. Readings stored
            readings = store.get_readings("J100000")
            assert len(readings) == 3
            assert readings[0].kwh == 48439.58


class TestSyncPartialFailures:
    @pytest.mark.asyncio
    async def test_page_failure_does_not_corrupt_prior_page_data(self, store: Store):
        """If page 2 fails with 500 error, page 1 data remains intact and audit records partial_failure."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/meters":
                page = request.url.params.get("page", "1")
                if page == "1":
                    p1_html = METERS_LIST_PAGE_HTML.replace("Page 1 of 21", "Page 1 of 2")
                    return httpx.Response(200, text=p1_html)
                if page == "2":
                    return httpx.Response(500, text="Internal Server Error")
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            max_retries=1,
            base_delay=0.01,
            jitter=0,
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            run = await engine.sync_meters_list(client)

            # Audit status is PARTIAL_FAILURE
            assert run.status == SyncStatus.PARTIAL_FAILURE
            assert run.items_new == 3  # Page 1 succeeded
            assert run.items_failed == 1  # Page 2 failed

            # Page 1 data is safe and intact in store
            assert store.count_meters() == 3
            assert store.get_meter("J100000") is not None
            assert store.get_meter("J100001") is not None
            assert store.get_meter("J100002") is not None

    @pytest.mark.asyncio
    async def test_meter_details_partial_failure_preserves_successful_meters(self, store: Store):
        """When batch syncing details, a failing meter does not crash or corrupt others."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/meters/J100000":
                return httpx.Response(200, text=METER_DETAIL_HTML)
            if request.url.path == "/meters/J100001":
                # Returns 500 error -> client raises error for this meter
                return httpx.Response(500, text="Server Error")
            if request.url.path == "/meters/J100002":
                html2 = METER_DETAIL_HTML.replace("J100000", "J100002")
                return httpx.Response(200, text=html2)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with PortalClient(
            base_url="https://test.portal",
            email="op@urja.local",
            password="secret",
            max_retries=1,
            base_delay=0.01,
            jitter=0,
            transport=transport,
        ) as client:
            engine = SyncEngine(store)
            run = await engine.sync_meter_details(client, ["J100000", "J100001", "J100002"])

            assert run.status == SyncStatus.PARTIAL_FAILURE
            assert run.items_new == 2
            assert run.items_failed == 1

            # J100000 and J100002 exist with full detail
            assert store.get_meter("J100000") is not None
            assert store.get_meter("J100002") is not None
            assert store.get_meter("J100001") is None

    @pytest.mark.asyncio
    async def test_sync_transformers(self, store: Store):
        """Transformers list is fetched and stored with audit trail."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            if request.url.path == "/transformers":
                return httpx.Response(200, text=TRANSFORMERS_LIST_HTML)
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
            assert run.items_failed == 0

            transformers = store.list_transformers()
            assert len(transformers) == 3
            assert transformers[0].code == "DT-001"
            assert transformers[0].capacity_kva == 100.0
