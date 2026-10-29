"""Freshness management and single-flight request coalescing.

Provides:
- TTL policies per resource type (master long, recent readings short, historical immutable)
- Single-flight deduplication: concurrent requests for the same resource execute exactly ONE portal call
- Metadata generation (fetched_at, age_seconds, stale, source)
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ResourceType(str, Enum):
    """Resource types with distinct freshness and caching characteristics."""

    METER_LIST = "meter_list"
    METER_DETAIL = "meter_detail"
    TRANSFORMERS = "transformers"
    CONSUMPTION_RECENT = "consumption_recent"
    CONSUMPTION_HISTORICAL = "consumption_historical"


# Default TTLs (in seconds)
# - Master data (nameplate, transformers, hierarchy): 1 hour (rarely changes)
# - Recent consumption (< 24h old): 5 minutes (30-min portal publishing intervals)
# - Historical consumption (> 24h old): 30 days (immutable once published)
DEFAULT_TTLS: dict[ResourceType, float] = {
    ResourceType.METER_LIST: 3600.0,
    ResourceType.METER_DETAIL: 3600.0,
    ResourceType.TRANSFORMERS: 86400.0,
    ResourceType.CONSUMPTION_RECENT: 300.0,
    ResourceType.CONSUMPTION_HISTORICAL: 2592000.0,  # 30 days (immutable)
}


class ResponseMeta(BaseModel):
    """Provenance and cache freshness metadata included on every API response."""

    fetched_at: datetime
    age_seconds: float
    stale: bool
    source: str  # "store" | "portal"


class SingleFlight:
    """Suppresses duplicate concurrent asynchronous operations.

    If 20 concurrent requests attempt to fetch the same expired resource,
    only ONE executes the underlying coroutine. The other 19 await the
    shared result.
    """

    def __init__(self) -> None:
        self._flights: dict[str, asyncio.Future[Any]] = {}
        self._lock: asyncio.Lock | None = None

    @property
    def lock(self) -> asyncio.Lock:
        """Lazily initialize lock in running event loop."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def do(
        self,
        key: str,
        coro_fn: Callable[[], Coroutine[Any, Any, T]],
    ) -> T:
        """Execute coro_fn once for the given key among concurrent callers."""
        async with self.lock:
            if key in self._flights:
                future = self._flights[key]
                is_leader = False
            else:
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                self._flights[key] = future
                is_leader = True

        if not is_leader:
            result = await future
            return result

        try:
            val = await coro_fn()
            future.set_result(val)
            return val
        except BaseException as exc:
            future.set_exception(exc)
            with contextlib.suppress(Exception):
                future.exception()
            raise
        finally:
            async with self.lock:
                self._flights.pop(key, None)


class FreshnessManager:
    """Evaluates age against TTL policies and coordinates single-flight refreshes."""

    def __init__(
        self,
        ttls: dict[ResourceType, float] | None = None,
    ) -> None:
        self.ttls = ttls or dict(DEFAULT_TTLS)
        self.single_flight = SingleFlight()

    def get_ttl(self, resource_type: ResourceType) -> float:
        """Return TTL in seconds for the given resource type."""
        return self.ttls.get(resource_type, 300.0)

    def evaluate(
        self,
        fetched_at: datetime,
        resource_type: ResourceType,
        source: str = "store",
        now: datetime | None = None,
    ) -> ResponseMeta:
        """Compute freshness metadata for an entity given its fetched_at timestamp."""
        now_utc = now or datetime.now(timezone.utc)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        else:
            fetched_at = fetched_at.astimezone(timezone.utc)

        age = max(0.0, (now_utc - fetched_at).total_seconds())
        ttl = self.get_ttl(resource_type)
        stale = age > ttl

        return ResponseMeta(
            fetched_at=fetched_at,
            age_seconds=round(age, 2),
            stale=stale,
            source=source,
        )
