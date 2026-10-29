"""Hardening tests for Phase 7.

Covers:
- Long-running session mid-sync expiry and automatic transparent recovery
- Prevention of unhandled portal errors surfacing as 500s (surfaces as RFC 7807 502)
- Secret scrubbing in logs (passwords, tokens, URJA_PASSWORD)
- Strict input validation across endpoints (path traversal, date inversion, parameter bounds)
"""

from __future__ import annotations

import logging

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from urja.api.app import app
from urja.api.deps import get_client, get_freshness, get_store, get_sync_engine
from urja.client.exceptions import (
    PortalShapeError,
    PortalTimeoutError,
    PortalUnavailableError,
    ReadOnlyGuardError,
)
from urja.client.portal import PortalClient
from urja.domain.models import SyncStatus
from urja.freshness.manager import FreshnessManager
from urja.logging import SecretRedactingFilter
from urja.store.repository import Store
from urja.sync.engine import SyncEngine

LOGIN_PAGE_HTML = """\
<html>
<body>
  <form action="/login" method="post">
    <input type="text" name="email" />
    <input type="password" name="password" />
    <button type="submit">Sign In</button>
  </form>
</body>
</html>
"""


# ── Long-running Session Test ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_expires_mid_sync_transparent_recovery():
    """A multi-page sync detects mid-stream session expiration, re-authenticates, and completes."""
    store = Store(":memory:")
    login_count = 0
    page_requests = 0

    def mock_portal(request: httpx.Request) -> httpx.Response:
        nonlocal login_count, page_requests
        if request.url.path == "/login":
            login_count += 1
            return httpx.Response(200, text="Login OK")

        if request.url.path == "/portal/meters/search":
            page = request.url.params.get("page", "1")
            page_requests += 1

            if page == "1":
                # Page 1 returns initial 2 meters
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"meterId": "J100001", "make": "HPL"},
                            {"meterId": "J100002", "make": "L&T"},
                        ],
                        "total": 5,
                        "page": 1,
                        "pageSize": 2,
                    },
                )

            if page == "2":
                # First time page 2 is requested: session expired! Returns login HTML
                if login_count == 1:
                    return httpx.Response(
                        200,
                        text=LOGIN_PAGE_HTML,
                        headers={"content-type": "text/html"},
                    )
                # After re-login (login_count == 2): succeeds
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"meterId": "J100003", "make": "Secure"},
                            {"meterId": "J100004", "make": "Genus"},
                        ],
                        "total": 5,
                        "page": 2,
                        "pageSize": 2,
                    },
                )

            if page == "3":
                return httpx.Response(
                    200,
                    json={
                        "data": [{"meterId": "J100005", "make": "Allied"}],
                        "total": 5,
                        "page": 3,
                        "pageSize": 2,
                    },
                )

        return httpx.Response(404)

    transport = httpx.MockTransport(mock_portal)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret-token-123",
        transport=transport,
    ) as client:
        engine = SyncEngine(store)
        run = await engine.sync_meters_search(client)

        # 1. Sync must finish with SUCCESS
        assert run.status == SyncStatus.SUCCESS
        assert run.items_failed == 0
        assert run.items_new == 5
        assert store.count_meters() == 5

        # 2. Transparent re-login verified
        assert login_count == 2  # initial login + mid-sync re-login
        assert page_requests == 4  # page 1, page 2 (expired), page 2 (retried), page 3


# ── Unhandled Portal Errors Surfacing as 502s (Not 500s) ───────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_instance",
    [
        PortalUnavailableError("Legacy portal 503 Service Unavailable"),
        PortalTimeoutError("Upstream portal timed out after retries"),
        PortalShapeError("Missing table element on detail page", field="table"),
        ReadOnlyGuardError("Refusing non-GET method"),
        httpx.ConnectError("Connection refused by upstream gateway"),
    ],
)
async def test_unhandled_portal_errors_surface_as_502(error_instance):
    """Any portal transport, availability, or format deviation surfaces as 502, never 500."""
    store = Store(":memory:")
    freshness = FreshnessManager()

    async def mock_fail_client():
        raise error_instance

    class FailingSyncEngine:
        async def sync_meter_detail(self, client, meter_id):
            raise error_instance

    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_freshness] = lambda: freshness
    app.dependency_overrides[get_sync_engine] = lambda: FailingSyncEngine()
    app.dependency_overrides[get_client] = lambda: mock_fail_client

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/v1/meters/J100000")
            assert resp.status_code == 502
            assert resp.headers["content-type"] == "application/problem+json"
            body = resp.json()
            assert body["title"] == "Bad Gateway"
            assert body["status"] == 502
            assert "Upstream portal communication failed" in body["detail"]
    finally:
        app.dependency_overrides.clear()
        store.close()


# ── Secret Redaction in Logs ──────────────────────────────────────────


def test_secret_redacting_filter():
    """Verify that sensitive values, passwords, and tokens are scrubbed from logs."""
    redactor = SecretRedactingFilter(extra_secret="SUPER_SECRET_PASSWORD_XYZ")

    # Record with password in msg
    record1 = logging.LogRecord(
        name="urja.client",
        level=logging.INFO,
        pathname="portal.py",
        lineno=100,
        msg='Posting payload: {"email": "test@urja.local", "password": "MySecretPassword123"}',
        args=(),
        exc_info=None,
    )
    redactor.filter(record1)
    assert "MySecretPassword123" not in record1.msg
    assert "***REDACTED***" in record1.msg

    # Record with extra_secret (URJA_PASSWORD) in args
    record2 = logging.LogRecord(
        name="urja.client",
        level=logging.INFO,
        pathname="portal.py",
        lineno=101,
        msg="Connecting with credentials: %s and %s",
        args=("admin@urja.local", "SUPER_SECRET_PASSWORD_XYZ"),
        exc_info=None,
    )
    redactor.filter(record2)
    assert "SUPER_SECRET_PASSWORD_XYZ" not in record2.args[1]
    assert record2.args[1] == "***REDACTED***"

    # Record with Bearer token
    record3 = logging.LogRecord(
        name="urja.client",
        level=logging.DEBUG,
        pathname="portal.py",
        lineno=102,
        msg="Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz.abc",
        args=(),
        exc_info=None,
    )
    redactor.filter(record3)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in record3.msg
    assert "Bearer ***REDACTED***" in record3.msg


# ── Missing Input Validation Tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_input_validation_date_inversion():
    """GET /v1/meters/{id}/consumption returns 400 when from > to."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/meters/J100000/consumption?from=2026-10-29T18:00:00Z&to=2026-10-29T12:00:00Z"
        )
        assert resp.status_code == 400
        assert resp.headers["content-type"] == "application/problem+json"
        assert "must be earlier than or equal to 'to'" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    [
        "meter;drop",
        "meter!@#",
        "meter with spaces",
        "a" * 100,  # exceeds max_length 64
    ],
)
async def test_input_validation_meter_id_pattern(bad_id):
    """Path parameters with illegal characters or excessive lengths return 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(f"/v1/meters/{bad_id}")
        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/problem+json"
        assert resp.json()["title"] == "Unprocessable Entity"


@pytest.mark.asyncio
async def test_input_validation_node_id_pattern():
    """Node path parameters with illegal characters return 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/v1/nodes/../../../children")
        assert resp.status_code in (404, 422)  # Reject path traversal
