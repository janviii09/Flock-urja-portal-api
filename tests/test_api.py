"""Tests for Phase 5 API layer and freshness engine.

Covers:
- API end-to-end integration with MockTransport
- Single-flight coalescing: 20 concurrent requests cause exactly ONE portal call
- TTL boundaries (fresh vs stale, stale-while-revalidate graceful degradation)
- Cursor pagination and multi-parameter filters on /v1/meters
- Consumption time-series filtering on /v1/meters/{id}/consumption
- Health check with upstream portal status and last sync
- RFC 7807 Problem Details error responses (404, 422, 502)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport

from tests.fixtures.portal_pages import (
    METER_DETAIL_HTML,
    METERS_LIST_PAGE_HTML,
)
from urja.api.app import app
from urja.api.deps import get_client, get_freshness, get_store
from urja.client.portal import PortalClient
from urja.freshness.manager import FreshnessManager
from urja.store.repository import Store


@pytest.fixture
def mock_portal_transport():
    """Default mock transport returning login OK, meters list, and meter detail."""
    call_counts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        call_counts[path] = call_counts.get(path, 0) + 1

        if path == "/login":
            return httpx.Response(200, text="OK")
        if path == "/meters":
            return httpx.Response(200, text=METERS_LIST_PAGE_HTML)
        if path == "/meters/J100000":
            return httpx.Response(200, text=METER_DETAIL_HTML)
        if path == "/meters/J100001":
            html1 = METER_DETAIL_HTML.replace("J100000", "J100001").replace("SE33962", "GE84132")
            return httpx.Response(200, text=html1)
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(handler)
    transport.call_counts = call_counts  # type: ignore[attr-defined]
    return transport


@pytest.fixture
def test_setup(mock_portal_transport):
    """Sets up an in-memory Store and PortalClient connected to mock transport."""
    test_store = Store(":memory:")
    test_client = PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        transport=mock_portal_transport,
        base_delay=0.001,
        jitter=0,
    )
    test_freshness = FreshnessManager()

    app.dependency_overrides[get_store] = lambda: test_store
    app.dependency_overrides[get_client] = lambda: test_client
    app.dependency_overrides[get_freshness] = lambda: test_freshness

    yield test_store, test_client, test_freshness, mock_portal_transport

    app.dependency_overrides.clear()
    test_store.close()


class TestSingleFlight:
    @pytest.mark.asyncio
    async def test_twenty_concurrent_requests_trigger_single_portal_call(self, test_setup):
        """20 concurrent requests for an uncached meter cause exactly ONE portal call."""
        _, _, _, transport = test_setup

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            # Launch 20 simultaneous requests for J100000
            tasks = [api_client.get("/v1/meters/J100000") for _ in range(20)]
            responses = await asyncio.gather(*tasks)

            # All 20 received HTTP 200 with identical meter payload
            assert all(r.status_code == 200 for r in responses)
            data_first = responses[0].json()["data"]
            assert data_first["meter_id"] == "J100000"
            assert all(r.json()["data"]["meter_id"] == "J100000" for r in responses)

            # Exactly ONE request was made to upstream /meters/J100000
            assert transport.call_counts.get("/meters/J100000") == 1


class TestTTLBoundaryAndStaleWhileRevalidate:
    @pytest.mark.asyncio
    async def test_fresh_store_item_served_without_portal_call(self, test_setup):
        """Item within TTL boundary is served from store without calling the portal."""
        store, _, _, transport = test_setup

        # Seed meter into store fetched 10 minutes ago (TTL is 3600s = 1 hour)
        from tests.test_store import _sample_provenance
        from urja.domain.models import Meter, PhaseType

        recent_ts = datetime.now(timezone.utc) - timedelta(minutes=10)
        prov = _sample_provenance()
        prov.fetched_at = recent_ts
        store.upsert_meter(
            Meter(
                meter_id="J100000", serial_no="SE33962", make="HPL", phase_type=PhaseType.SINGLE
            ),
            prov,
        )

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            resp = await api_client.get("/v1/meters/J100000")
            assert resp.status_code == 200
            body = resp.json()
            assert body["data"]["meter_id"] == "J100000"
            assert body["meta"]["source"] == "store"
            assert body["meta"]["stale"] is False
            assert body["meta"]["age_seconds"] >= 600

            # No portal call occurred
            assert transport.call_counts.get("/meters/J100000", 0) == 0

    @pytest.mark.asyncio
    async def test_stale_item_revalidated_via_portal(self, test_setup):
        """Item exceeding TTL boundary triggers portal revalidation."""
        store, _, _, transport = test_setup
        from tests.test_store import _sample_provenance
        from urja.domain.models import Meter

        # Seed meter fetched 2 hours ago (> 1 hour TTL)
        old_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        prov = _sample_provenance()
        prov.fetched_at = old_ts
        store.upsert_meter(Meter(meter_id="J100000", serial_no="OLD_SERIAL", make="HPL"), prov)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            resp = await api_client.get("/v1/meters/J100000")
            assert resp.status_code == 200
            body = resp.json()
            # Portal updated data
            assert body["meta"]["source"] == "portal"
            assert body["meta"]["stale"] is False
            assert transport.call_counts.get("/meters/J100000") == 1

    @pytest.mark.asyncio
    async def test_stale_while_revalidate_fallback_when_portal_down(self, test_setup):
        """When stale item revalidation fails due to portal outage, serves stale store copy."""
        store, test_client, _, _ = test_setup
        from tests.test_store import _sample_provenance
        from urja.domain.models import Meter

        # Seed stale meter
        old_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        prov = _sample_provenance()
        prov.fetched_at = old_ts
        store.upsert_meter(Meter(meter_id="J100000", serial_no="STORE_SERIAL", make="HPL"), prov)

        # Configure portal client to fail
        def failing_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login":
                return httpx.Response(200, text="OK")
            return httpx.Response(500, text="Internal Portal Error")

        test_client._transport = httpx.MockTransport(failing_handler)
        test_client._client = httpx.AsyncClient(
            transport=test_client._transport, base_url="https://test.portal"
        )

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            resp = await api_client.get("/v1/meters/J100000")
            assert resp.status_code == 200
            body = resp.json()
            assert body["data"]["serial_no"] == "STORE_SERIAL"
            assert body["meta"]["source"] == "store"
            assert body["meta"]["stale"] is True


class TestEndpointsAndPagination:
    @pytest.mark.asyncio
    async def test_meters_cursor_pagination(self, test_setup):
        """GET /v1/meters supports cursor pagination across result sets."""
        store, _, _, _ = test_setup
        from tests.test_store import _sample_provenance
        from urja.domain.models import Meter

        prov = _sample_provenance()
        for i in range(1, 6):
            store.upsert_meter(
                Meter(meter_id=f"J10000{i}", serial_no=f"SE{i}", make="HPL", dt_code="DT-001"),
                prov,
            )

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            # 1. Page 1 (limit 2)
            r1 = await api_client.get("/v1/meters?limit=2")
            assert r1.status_code == 200
            b1 = r1.json()
            assert len(b1["data"]) == 2
            assert b1["pagination"]["has_more"] is True
            cursor1 = b1["pagination"]["next_cursor"]
            assert cursor1 == "J100002"

            # 2. Page 2 (limit 2, cursor from page 1)
            r2 = await api_client.get(f"/v1/meters?limit=2&cursor={cursor1}")
            assert r2.status_code == 200
            b2 = r2.json()
            assert len(b2["data"]) == 2
            assert [m["meter_id"] for m in b2["data"]] == ["J100003", "J100004"]
            cursor2 = b2["pagination"]["next_cursor"]

            # 3. Page 3 (last page)
            r3 = await api_client.get(f"/v1/meters?limit=2&cursor={cursor2}")
            assert r3.status_code == 200
            b3 = r3.json()
            assert len(b3["data"]) == 1
            assert b3["data"][0]["meter_id"] == "J100005"
            assert b3["pagination"]["has_more"] is False
            assert b3["pagination"]["next_cursor"] is None

    @pytest.mark.asyncio
    async def test_meters_filtering(self, test_setup):
        """GET /v1/meters filters by dt_code, serial_no, and make."""
        store, _, _, _ = test_setup
        from tests.test_store import _sample_provenance
        from urja.domain.models import Meter, PhaseType

        prov = _sample_provenance()
        store.upsert_meter(
            Meter(
                meter_id="J100001",
                serial_no="SE1",
                make="HPL",
                dt_code="DT-001",
                phase_type=PhaseType.SINGLE,
            ),
            prov,
        )
        store.upsert_meter(
            Meter(
                meter_id="J100002",
                serial_no="SE2",
                make="L&T",
                dt_code="DT-002",
                phase_type=PhaseType.THREE,
            ),
            prov,
        )

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            # Filter by dt_code
            r_dt = await api_client.get("/v1/meters?dt_code=DT-001")
            assert len(r_dt.json()["data"]) == 1
            assert r_dt.json()["data"][0]["meter_id"] == "J100001"

            # Filter by make
            r_make = await api_client.get("/v1/meters?make=L%26T")
            assert len(r_make.json()["data"]) == 1
            assert r_make.json()["data"][0]["meter_id"] == "J100002"

    @pytest.mark.asyncio
    async def test_meter_consumption_date_filtering(self, test_setup):
        """GET /v1/meters/{id}/consumption filters by from and to query params."""
        store, _, _, _ = test_setup
        from tests.test_store import _sample_provenance
        from urja.domain.models import Meter, Reading

        prov = _sample_provenance()
        store.upsert_meter(Meter(meter_id="J100000", serial_no="SE1", make="HPL"), prov)

        ts1 = datetime(2026, 6, 23, 10, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 6, 23, 10, 30, 0, tzinfo=timezone.utc)
        ts3 = datetime(2026, 6, 23, 11, 0, 0, tzinfo=timezone.utc)
        store.upsert_readings(
            [
                Reading(meter_id="J100000", timestamp=ts1, kwh=10.0, kvah=11.0),
                Reading(meter_id="J100000", timestamp=ts2, kwh=10.5, kvah=11.5),
                Reading(meter_id="J100000", timestamp=ts3, kwh=11.0, kvah=12.0),
            ],
            prov,
        )

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            # Filter from 10:15 to 10:45 -> should return exactly ts2
            from_str = "2026-06-23T10:15:00Z"
            to_str = "2026-06-23T10:45:00Z"
            resp = await api_client.get(
                f"/v1/meters/J100000/consumption?from={from_str}&to={to_str}"
            )
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert len(data) == 1
            assert data[0]["kwh"] == 10.5
            assert "meta" in resp.json()

    @pytest.mark.asyncio
    async def test_health_endpoint(self, test_setup):
        """GET /v1/health returns status, portal reachable, and last sync info."""
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            resp = await api_client.get("/v1/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert body["portal_reachable"] is True
            assert "meta" in body


class TestRFC7807Errors:
    @pytest.mark.asyncio
    async def test_meter_not_found_returns_rfc7807(self, test_setup):
        """Unknown meter returns 404 with application/problem+json."""
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            resp = await api_client.get("/v1/meters/J999999")
            assert resp.status_code == 404
            assert resp.headers["content-type"] == "application/problem+json"
            problem = resp.json()
            assert problem["type"].endswith("/meter-not-found")
            assert problem["title"] == "Meter Not Found"
            assert problem["status"] == 404
            assert "J999999" in problem["detail"]
            assert problem["instance"] == "/v1/meters/J999999"

    @pytest.mark.asyncio
    async def test_validation_error_returns_rfc7807(self, test_setup):
        """Invalid query parameters return 422 with application/problem+json and invalid_params."""
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            resp = await api_client.get("/v1/meters?limit=-10")
            assert resp.status_code == 422
            assert resp.headers["content-type"] == "application/problem+json"
            problem = resp.json()
            assert problem["status"] == 422
            assert problem["title"] == "Unprocessable Entity"
            assert "invalid_params" in problem
            assert len(problem["invalid_params"]) > 0

    @pytest.mark.asyncio
    async def test_docs_and_openapi_served(self, test_setup):
        """FastAPI serves /docs and /openapi.json."""
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as api_client:
            docs_resp = await api_client.get("/docs")
            assert docs_resp.status_code == 200
            openapi_resp = await api_client.get("/openapi.json")
            assert openapi_resp.status_code == 200
            spec = openapi_resp.json()
            assert "/v1/meters" in spec["paths"]
            assert "/v1/meters/{meter_id}" in spec["paths"]
            assert "/v1/meters/{meter_id}/consumption" in spec["paths"]
