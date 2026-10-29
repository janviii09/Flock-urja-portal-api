"""Urja legacy portal HTTP client.

Features:
- SvelteKit session handling and login flow.
- Automatic detection of expired sessions + single re-auth retry.
- Exponential backoff with jitter on transient network/5xx errors.
- Enforced concurrency limit via asyncio.Semaphore.
- Read-only guard (refuses non-GET requests from consumers).
- Raw response disk capture for fixture recording.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from urja.client.exceptions import (
    AuthenticationError,
    PortalRequestError,
    PortalTimeoutError,
    PortalUnavailableError,
    ReadOnlyGuardError,
)
from urja.settings import settings

logger = logging.getLogger(__name__)

TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class PortalClient:
    """Async client for interacting with the legacy Urja Meter Ops portal."""

    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
        concurrency_limit: int | None = None,
        max_retries: int | None = None,
        base_delay: float = 0.5,
        max_delay: float = 5.0,
        jitter: float = 0.25,
        record_captures: bool | None = None,
        capture_dir: str | Path | None = None,
        captures_dir: str | Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.urja_base_url).rstrip("/")
        self.email = email if email is not None else settings.urja_email
        self.password = password if password is not None else settings.urja_password
        self.timeout = timeout if timeout is not None else settings.portal_timeout
        self.concurrency_limit = (
            concurrency_limit if concurrency_limit is not None else settings.portal_concurrency
        )
        self.max_retries = max_retries if max_retries is not None else settings.portal_max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.record_captures = (
            record_captures if record_captures is not None else settings.portal_record_captures
        )
        effective_capture_dir = capture_dir or captures_dir or settings.portal_captures_dir
        self.capture_dir = Path(effective_capture_dir)

        self._semaphore: asyncio.Semaphore | None = None
        self._is_logged_in = False
        self._auth_lock: asyncio.Lock | None = None

        # Internal HTTP client
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": "UrjaApiClient/0.1.0 (+https://urja-ops.flockenergy.tech)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            },
        )

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Lazily initialize concurrency semaphore in active event loop."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency_limit)
        return self._semaphore

    @property
    def auth_lock(self) -> asyncio.Lock:
        """Lazily initialize authentication lock in active event loop."""
        if self._auth_lock is None:
            self._auth_lock = asyncio.Lock()
        return self._auth_lock

    async def __aenter__(self) -> PortalClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close underlying HTTP client."""
        await self._client.aclose()

    # ── Authentication ────────────────────────────────────────────────

    async def login(self) -> None:
        """Perform SvelteKit form login on the portal."""
        async with self.auth_lock:
            if not self.email or not self.password:
                raise AuthenticationError(
                    "Missing portal credentials: URJA_EMAIL and URJA_PASSWORD must be configured."
                )

            logger.info("Authenticating with Urja Meter Ops portal as %s", self.email)
            try:
                # SvelteKit form action: POST /login with form fields email & password
                resp = await self._client.post(
                    "/login",
                    data={"email": self.email, "password": self.password},
                )
            except httpx.TransportError as exc:
                raise PortalUnavailableError(
                    f"Login failed due to transport error: {exc}"
                ) from exc

            # Verify response
            if resp.status_code in (401, 403):
                raise AuthenticationError(
                    f"Authentication failed: HTTP {resp.status_code} for user {self.email}"
                )

            # Check if SvelteKit rejected login and re-rendered the login form
            resp_url_path = str(resp.url).rstrip("/").split(self.base_url)[-1]
            if resp_url_path == "/login" and 'name="password"' in resp.text:
                raise AuthenticationError(
                    f"Authentication failed: portal re-rendered login form for {self.email}"
                )

            self._is_logged_in = True
            logger.info("Successfully authenticated with Urja Meter Ops portal.")

    def _is_session_expired(self, response: httpx.Response, requested_path: str) -> bool:
        """Check if response indicates an expired or missing session."""
        if response.status_code in (401, 403):
            return True

        # SvelteKit redirect to /login when unauthenticated
        norm_path = requested_path.split("?")[0].rstrip("/")
        if norm_path not in ("", "/login"):
            resp_url = str(response.url).rstrip("/")
            if resp_url.endswith("/login"):
                return True
            if 'name="password"' in response.text and 'name="email"' in response.text:
                return True

        return False

    # ── Read-only guard & execution ───────────────────────────────────

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Execute HTTP request with strict read-only enforcement."""
        if method.upper() != "GET":
            raise ReadOnlyGuardError(
                f"Portal is strictly read-only; refusing {method.upper()} request to '{url}'."
            )
        return await self.get(url, **kwargs)

    async def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute GET request with concurrency limiting, retry backoff, and re-auth."""
        # Ensure initial login
        if not self._is_logged_in:
            await self.login()

        # Attempt request with exponential backoff on transient errors
        response = await self._execute_with_retry("GET", url, params=params, headers=headers)

        # Check for expired session
        if self._is_session_expired(response, url):
            logger.warning("Portal session expired during GET %s. Re-authenticating...", url)
            self._is_logged_in = False
            await self.login()

            # Retry once after re-authenticating
            response = await self._execute_with_retry("GET", url, params=params, headers=headers)
            if self._is_session_expired(response, url):
                raise AuthenticationError(
                    f"Session expired and re-authentication failed for GET {url}"
                )

        # Optionally record raw response to disk for fixtures
        if self.record_captures and response.status_code == 200:
            self._record_capture(url, response)

        return response

    async def _execute_with_retry(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute request under concurrency semaphore with exponential backoff + jitter."""
        last_exception: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                async with self.semaphore:
                    response = await self._client.request(
                        method,
                        url,
                        params=params,
                        headers=headers,
                    )

                if response.status_code in TRANSIENT_STATUS_CODES:
                    if attempt == self.max_retries:
                        raise PortalUnavailableError(
                            f"HTTP {response.status_code} transient error after {self.max_retries} attempts: {url}"
                        )
                    await self._sleep_backoff(attempt)
                    continue

                return response

            except httpx.TimeoutException as exc:
                last_exception = exc
                logger.warning(
                    "Timeout on %s %s (attempt %d/%d): %s",
                    method,
                    url,
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt == self.max_retries:
                    raise PortalTimeoutError(
                        f"Request timed out after {self.max_retries} attempts: {url}"
                    ) from exc
                await self._sleep_backoff(attempt)

            except httpx.TransportError as exc:
                last_exception = exc
                logger.warning(
                    "Transport error on %s %s (attempt %d/%d): %s",
                    method,
                    url,
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt == self.max_retries:
                    raise PortalUnavailableError(
                        f"Transport error after {self.max_retries} attempts: {url}"
                    ) from exc
                await self._sleep_backoff(attempt)

        # Fallback if loop finishes unexpectedly
        if last_exception:
            raise PortalRequestError(f"Request failed: {last_exception}") from last_exception
        raise PortalRequestError(f"Request failed after {self.max_retries} retries: {url}")

    async def _sleep_backoff(self, attempt: int) -> None:
        """Calculate exponential backoff with jitter and sleep."""
        delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        if self.jitter > 0:
            delay += random.uniform(0, self.jitter)
        await asyncio.sleep(delay)

    # ── Raw Capture Recording ─────────────────────────────────────────

    def _record_capture(self, url: str, response: httpx.Response) -> None:
        """Save raw portal response to disk for fixture generation (gitignored)."""
        try:
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            slug = re.sub(r"[^a-zA-Z0-9_-]", "_", url.strip("/")).strip("_") or "root"

            content_type = response.headers.get("content-type", "")
            ext = ".json" if "json" in content_type else ".html"

            target = self.capture_dir / f"{timestamp}_{slug}{ext}"
            target.write_bytes(response.content)
            logger.debug("Recorded portal capture to %s", target)
        except Exception as exc:
            logger.warning("Failed to record portal capture for %s: %s", url, exc)
