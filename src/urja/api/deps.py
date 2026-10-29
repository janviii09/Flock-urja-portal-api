"""Dependency injection providers for FastAPI application."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from urja.client.portal import PortalClient
from urja.freshness.manager import FreshnessManager
from urja.settings import settings
from urja.store.repository import Store
from urja.sync.engine import SyncEngine

# Shared singleton instances for application runtime
_store: Store | None = None
_client: PortalClient | None = None
_freshness: FreshnessManager | None = None
_sync_engine: SyncEngine | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(db_path=settings.db_path)
    return _store


def get_client() -> PortalClient:
    global _client
    if _client is None:
        _client = PortalClient(
            base_url=settings.urja_base_url,
            email=settings.urja_email,
            password=settings.urja_password,
            timeout=settings.portal_timeout,
            concurrency_limit=settings.portal_concurrency,
            max_retries=settings.portal_max_retries,
            record_captures=settings.portal_record_captures,
            captures_dir=settings.portal_captures_dir,
        )
    return _client


def get_freshness() -> FreshnessManager:
    global _freshness
    if _freshness is None:
        _freshness = FreshnessManager()
    return _freshness


def get_sync_engine(
    store: Annotated[Store, Depends(get_store)],
) -> SyncEngine:
    global _sync_engine
    if _sync_engine is None or _sync_engine.store != store:
        _sync_engine = SyncEngine(store=store)
    return _sync_engine
