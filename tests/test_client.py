"""Tests for Urja portal HTTP client using httpx.MockTransport."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.fixtures.portal_pages import (
    LOGIN_FAILED_HTML,
    LOGIN_PAGE_HTML,
    METERS_LIST_PAGE_HTML,
)
from urja.client import (
    AuthenticationError,
    PortalClient,
    PortalTimeoutError,
    PortalUnavailableError,
    ReadOnlyGuardError,
)


@pytest.mark.asyncio
async def test_successful_login_and_get():
    """Client automatically logs in on first GET and returns response."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/login" and request.method == "POST":
            return httpx.Response(200, headers={"Set-Cookie": "session=xyz123"}, text="OK")
        if request.url.path == "/meters" and request.method == "GET":
            return httpx.Response(200, text=METERS_LIST_PAGE_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        transport=transport,
        base_delay=0.01,
        jitter=0,
    ) as client:
        resp = await client.get("/meters")
        assert resp.status_code == 200
        assert "J100000" in resp.text
        assert calls == [("POST", "/login"), ("GET", "/meters")]


@pytest.mark.asyncio
async def test_expired_session_triggers_relogin_and_retry_once():
    """Detect expired session (401 or redirect to login), re-login and retry ONCE."""
    step = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal step
        if request.url.path == "/login" and request.method == "POST":
            return httpx.Response(200, headers={"Set-Cookie": "session=new_session"})

        if request.url.path == "/meters" and request.method == "GET":
            step += 1
            if step == 1:
                # Simulates expired session via 401
                return httpx.Response(401, text="Unauthorized")
            # After re-login, succeeds
            return httpx.Response(200, text=METERS_LIST_PAGE_HTML)

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        transport=transport,
        base_delay=0.01,
        jitter=0,
    ) as client:
        # Pre-mark as logged in
        client._is_logged_in = True

        resp = await client.get("/meters")
        assert resp.status_code == 200
        assert "J100000" in resp.text
        assert step == 2


@pytest.mark.asyncio
async def test_expired_session_redirect_to_login_triggers_reauth():
    """Detect expired session when portal redirects to login page with HTML form."""
    step = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal step
        if request.url.path == "/login" and request.method == "POST":
            return httpx.Response(200, text="Login OK")

        if request.url.path == "/meters":
            step += 1
            if step == 1:
                # Returns 200 but content is login page HTML
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/html"},
                    text=LOGIN_PAGE_HTML,
                    request=httpx.Request("GET", "https://test.portal/login"),
                )
            return httpx.Response(200, text=METERS_LIST_PAGE_HTML)

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        transport=transport,
        base_delay=0.01,
        jitter=0,
    ) as client:
        client._is_logged_in = True
        resp = await client.get("/meters")
        assert resp.status_code == 200
        assert "J100000" in resp.text
        assert step == 2


@pytest.mark.asyncio
async def test_persistent_session_failure_raises_authentication_error():
    """If re-login still results in expired session, raise AuthenticationError."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login" and request.method == "POST":
            return httpx.Response(200, text="Login OK")
        if request.url.path == "/meters":
            return httpx.Response(401, text="Unauthorized")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        transport=transport,
        base_delay=0.01,
        jitter=0,
    ) as client:
        client._is_logged_in = True
        with pytest.raises(AuthenticationError, match="Session expired"):
            await client.get("/meters")


@pytest.mark.asyncio
async def test_5xx_transient_retry_with_eventual_success():
    """Transient 5xx errors retry with exponential backoff and succeed."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/login":
            return httpx.Response(200, text="Login OK")
        if request.url.path == "/meters":
            attempts += 1
            if attempts < 3:
                return httpx.Response(502, text="Bad Gateway")
            return httpx.Response(200, text=METERS_LIST_PAGE_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        max_retries=3,
        base_delay=0.01,
        jitter=0,
        transport=transport,
    ) as client:
        resp = await client.get("/meters")
        assert resp.status_code == 200
        assert attempts == 3


@pytest.mark.asyncio
async def test_5xx_retry_cap_exhaustion():
    """Transient 5xx errors exceeding max_retries raise PortalUnavailableError."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/login":
            return httpx.Response(200, text="Login OK")
        if request.url.path == "/meters":
            attempts += 1
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        max_retries=3,
        base_delay=0.01,
        jitter=0,
        transport=transport,
    ) as client:
        with pytest.raises(PortalUnavailableError, match="HTTP 503"):
            await client.get("/meters")
        assert attempts == 3


@pytest.mark.asyncio
async def test_timeout_retry_and_eventual_success():
    """Request timeouts retry and succeed if subsequent attempt passes."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/login":
            return httpx.Response(200, text="Login OK")
        if request.url.path == "/meters":
            attempts += 1
            if attempts == 1:
                raise httpx.ReadTimeout("Timeout reading socket", request=request)
            return httpx.Response(200, text=METERS_LIST_PAGE_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        max_retries=3,
        base_delay=0.01,
        jitter=0,
        transport=transport,
    ) as client:
        resp = await client.get("/meters")
        assert resp.status_code == 200
        assert attempts == 2


@pytest.mark.asyncio
async def test_timeout_retry_cap_exhaustion():
    """Timeouts exceeding max_retries raise PortalTimeoutError."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/login":
            return httpx.Response(200, text="Login OK")
        if request.url.path == "/meters":
            attempts += 1
            raise httpx.ConnectTimeout("Connection timed out", request=request)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        max_retries=3,
        base_delay=0.01,
        jitter=0,
        transport=transport,
    ) as client:
        with pytest.raises(PortalTimeoutError, match="timed out"):
            await client.get("/meters")
        assert attempts == 3


@pytest.mark.asyncio
async def test_read_only_guard_rejects_non_get():
    """Portal client strictly refuses non-GET requests."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        transport=transport,
    ) as client:
        with pytest.raises(ReadOnlyGuardError, match="refusing POST request"):
            await client.request("POST", "/meters")

        with pytest.raises(ReadOnlyGuardError, match="refusing DELETE request"):
            await client.request("DELETE", "/meters/J100000")


@pytest.mark.asyncio
async def test_record_captures_saves_raw_response(tmp_path):
    """When record_captures=True, raw responses are saved to disk."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, text="Login OK")
        if request.url.path == "/meters/J100000":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                text="<html>Detail</html>",
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        record_captures=True,
        capture_dir=tmp_path,
        transport=transport,
    ) as client:
        resp = await client.get("/meters/J100000")
        assert resp.status_code == 200

        files = list(tmp_path.glob("*.html"))
        assert len(files) == 1
        assert "meters_J100000.html" in files[0].name
        assert files[0].read_text() == "<html>Detail</html>"


@pytest.mark.asyncio
async def test_invalid_credentials_raises_authentication_error():
    """Failed login raises AuthenticationError."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                200,
                text=LOGIN_FAILED_HTML,
                request=httpx.Request("POST", "https://test.portal/login"),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="wrong@urja.local",
        password="bad",
        transport=transport,
    ) as client:
        with pytest.raises(AuthenticationError, match="Authentication failed"):
            await client.login()


@pytest.mark.asyncio
async def test_missing_credentials_raises_authentication_error():
    """Login with empty credentials raises AuthenticationError immediately."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    async with PortalClient(
        base_url="https://test.portal",
        email="",
        password="",
        transport=transport,
    ) as client:
        with pytest.raises(AuthenticationError, match="Missing portal credentials"):
            await client.login()


@pytest.mark.asyncio
async def test_session_expiry_detected_by_html_body_content():
    """Session expiry detected when 200 response body contains login form fields."""
    step = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal step
        if request.url.path == "/login" and request.method == "POST":
            return httpx.Response(200, text="Login OK")

        if request.url.path == "/meters":
            step += 1
            if step == 1:
                # 200 OK but body contains login form (no URL redirect)
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/html"},
                    text=LOGIN_PAGE_HTML,
                )
            return httpx.Response(200, text=METERS_LIST_PAGE_HTML)

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        transport=transport,
        base_delay=0.01,
        jitter=0,
    ) as client:
        client._is_logged_in = True
        resp = await client.get("/meters")
        assert resp.status_code == 200
        assert "J100000" in resp.text
        assert step == 2


@pytest.mark.asyncio
async def test_transport_error_retry_with_eventual_success():
    """Transport errors (e.g. ConnectionError) retry with backoff and succeed."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/login":
            return httpx.Response(200, text="Login OK")
        if request.url.path == "/meters":
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError("Connection refused", request=request)
            return httpx.Response(200, text=METERS_LIST_PAGE_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        max_retries=3,
        base_delay=0.01,
        jitter=0,
        transport=transport,
    ) as client:
        resp = await client.get("/meters")
        assert resp.status_code == 200
        assert attempts == 3


@pytest.mark.asyncio
async def test_transport_error_retry_cap_raises_unavailable():
    """Transport errors exceeding max_retries raise PortalUnavailableError."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/login":
            return httpx.Response(200, text="Login OK")
        if request.url.path == "/meters":
            attempts += 1
            raise httpx.ConnectError("Connection refused", request=request)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        max_retries=3,
        base_delay=0.01,
        jitter=0,
        transport=transport,
    ) as client:
        with pytest.raises(PortalUnavailableError, match="Transport error"):
            await client.get("/meters")
        assert attempts == 3


@pytest.mark.asyncio
async def test_429_rate_limit_retry():
    """429 Too Many Requests is treated as transient and retried."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/login":
            return httpx.Response(200, text="Login OK")
        if request.url.path == "/meters":
            attempts += 1
            if attempts < 2:
                return httpx.Response(429, text="Rate limited")
            return httpx.Response(200, text=METERS_LIST_PAGE_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        max_retries=3,
        base_delay=0.01,
        jitter=0,
        transport=transport,
    ) as client:
        resp = await client.get("/meters")
        assert resp.status_code == 200
        assert attempts == 2


@pytest.mark.asyncio
async def test_concurrency_semaphore_limits_parallel_requests():
    """Concurrency semaphore enforces max parallel requests."""
    max_concurrent = 0
    current_concurrent = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal max_concurrent, current_concurrent
        if request.url.path == "/login":
            return httpx.Response(200, text="Login OK")
        current_concurrent += 1
        max_concurrent = max(max_concurrent, current_concurrent)
        await asyncio.sleep(0.02)
        current_concurrent -= 1
        return httpx.Response(200, text=METERS_LIST_PAGE_HTML)

    transport = httpx.MockTransport(handler)
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        concurrency_limit=2,
        transport=transport,
        base_delay=0.01,
        jitter=0,
    ) as client:
        client._is_logged_in = True
        tasks = [client.get(f"/meters?page={i}") for i in range(6)]
        responses = await asyncio.gather(*tasks)
        assert all(r.status_code == 200 for r in responses)
        assert max_concurrent <= 2


@pytest.mark.asyncio
async def test_read_only_guard_rejects_all_mutating_methods():
    """Portal client refuses PUT, PATCH, DELETE, HEAD (non-GET) requests."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    async with PortalClient(
        base_url="https://test.portal",
        email="op@urja.local",
        password="secret",
        transport=transport,
    ) as client:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(ReadOnlyGuardError, match=f"refusing {method} request"):
                await client.request(method, "/meters")
