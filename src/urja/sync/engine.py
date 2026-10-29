"""Synchronization engine — orchestrates portal scraping, adapter parsing, and store ingestion.

Supports:
- Paginated sync of `/meters` (or bulk path if available)
- Detail sync of `/meters/{meter_id}` (nameplate, 7-level hierarchy, consumption readings)
- Transformers sync from `/transformers`
- Change detection and idempotent upserts
- Resilient isolation: per-meter/per-page failures never corrupt existing data
- Audit logging via ``sync_runs`` table
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from urja.adapters import (
    parse_meter_detail,
    parse_meters_list,
    parse_pagination,
    parse_transformers,
)
from urja.client.exceptions import PortalError, PortalShapeError
from urja.client.portal import PortalClient
from urja.domain.models import (
    Provenance,
    SyncRun,
    SyncStatus,
    UpsertResult,
)
from urja.store.repository import Store

logger = logging.getLogger(__name__)


def compute_payload_hash(raw_text: str) -> str:
    """Compute SHA-256 hex digest of portal response text."""
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


class SyncEngine:
    """Coordinates fetching from PortalClient and ingestion into Store."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def sync_meters_list(
        self,
        client: PortalClient,
        max_pages: int | None = None,
    ) -> SyncRun:
        """Paginate through /meters and synchronize all meter list summaries.

        Uses the pagination observed on portal (?page=1, ?page=2, ... 20 per page).
        Continues until all pages are retrieved or ``max_pages`` is reached.
        """
        run = self.store.create_sync_run(resource="meters_list")
        items_new = 0
        items_changed = 0
        items_unchanged = 0
        items_failed = 0
        current_page = 1
        total_pages = 1

        try:
            while True:
                if max_pages is not None and current_page > max_pages:
                    break

                endpoint = f"/meters?page={current_page}"
                try:
                    resp = await client.get(endpoint)
                    payload_hash = compute_payload_hash(resp.text)
                    fetched_at = datetime.now(timezone.utc)
                    provenance = Provenance(
                        fetched_at=fetched_at,
                        source_endpoint=endpoint,
                        payload_hash=payload_hash,
                    )

                    meters = parse_meters_list(resp.text)
                    _, page_total = parse_pagination(resp.text)
                    if page_total > total_pages:
                        total_pages = page_total

                    for meter in meters:
                        try:
                            res = self.store.upsert_meter(meter, provenance)
                            if res == UpsertResult.NEW:
                                items_new += 1
                            elif res == UpsertResult.CHANGED:
                                items_changed += 1
                            else:
                                items_unchanged += 1
                        except Exception as e:
                            logger.error("Failed to upsert meter %s: %s", meter.meter_id, e)
                            items_failed += 1

                except (PortalError, PortalShapeError, Exception) as e:
                    logger.error("Failed to fetch/parse page %d: %s", current_page, e)
                    items_failed += 1

                if current_page >= total_pages:
                    break
                current_page += 1

            status = SyncStatus.PARTIAL_FAILURE if items_failed > 0 else SyncStatus.SUCCESS
            finished_run = self.store.finish_sync_run(
                run_id=run.run_id,
                status=status,
                items_new=items_new,
                items_changed=items_changed,
                items_unchanged=items_unchanged,
                items_failed=items_failed,
            )
            return finished_run or run

        except Exception as e:
            logger.exception("Critical error during meters list sync: %s", e)
            finished_run = self.store.finish_sync_run(
                run_id=run.run_id,
                status=SyncStatus.FAILED,
                items_new=items_new,
                items_changed=items_changed,
                items_unchanged=items_unchanged,
                items_failed=items_failed + 1,
                error_message=str(e),
            )
            return finished_run or run

    async def sync_meter_detail(
        self,
        client: PortalClient,
        meter_id: str,
    ) -> tuple[UpsertResult, int, int]:
        """Fetch and ingest full detail for a single meter.

        Ingests nameplate, coordinates, 7-level hierarchy path, and consumption readings.
        Runs in an atomic per-meter transaction — partial failure never corrupts existing data.
        Returns (meter_upsert_result, readings_count, hierarchy_nodes_count).
        """
        endpoint = f"/meters/{meter_id}"
        resp = await client.get(endpoint)
        payload_hash = compute_payload_hash(resp.text)
        fetched_at = datetime.now(timezone.utc)
        provenance = Provenance(
            fetched_at=fetched_at,
            source_endpoint=endpoint,
            payload_hash=payload_hash,
        )

        detail = parse_meter_detail(resp.text, meter_id=meter_id)

        # Isolated per-meter transaction
        with self.store._lock:
            self.store._conn.execute("BEGIN IMMEDIATE")
            try:
                # 1. Upsert meter with coordinates and hierarchy JSON
                meter_res = self.store.upsert_meter(detail.meter, provenance)

                # 2. Upsert hierarchy nodes
                nodes_upserted = 0
                if detail.meter.hierarchy_path:
                    nodes = [
                        detail.meter.hierarchy_path.zone,
                        detail.meter.hierarchy_path.circle,
                        detail.meter.hierarchy_path.division,
                        detail.meter.hierarchy_path.subdivision,
                        detail.meter.hierarchy_path.substation,
                        detail.meter.hierarchy_path.feeder,
                        detail.meter.hierarchy_path.dt,
                    ]
                    for node in nodes:
                        if node is not None:
                            self.store.upsert_hierarchy_node(node, provenance)
                            nodes_upserted += 1

                # 3. Upsert readings
                for reading in detail.readings:
                    self.store.upsert_reading(reading, provenance)

                self.store._conn.execute("COMMIT")
                return meter_res, len(detail.readings), nodes_upserted

            except Exception:
                self.store._conn.execute("ROLLBACK")
                raise

    async def sync_meter_details(
        self,
        client: PortalClient,
        meter_ids: list[str],
    ) -> SyncRun:
        """Batch synchronize details for multiple meters with fault-tolerance."""
        run = self.store.create_sync_run(resource="meter_detail")
        items_new = 0
        items_changed = 0
        items_unchanged = 0
        items_failed = 0

        for mid in meter_ids:
            try:
                m_res, _, _ = await self.sync_meter_detail(client, mid)
                if m_res == UpsertResult.NEW:
                    items_new += 1
                elif m_res == UpsertResult.CHANGED:
                    items_changed += 1
                else:
                    items_unchanged += 1
            except Exception as e:
                logger.error("Failed to sync meter detail for %s: %s", mid, e)
                items_failed += 1

        status = SyncStatus.PARTIAL_FAILURE if items_failed > 0 else SyncStatus.SUCCESS
        finished_run = self.store.finish_sync_run(
            run_id=run.run_id,
            status=status,
            items_new=items_new,
            items_changed=items_changed,
            items_unchanged=items_unchanged,
            items_failed=items_failed,
        )
        return finished_run or run

    async def sync_transformers(
        self,
        client: PortalClient,
    ) -> SyncRun:
        """Synchronize transformers list from /transformers."""
        run = self.store.create_sync_run(resource="transformers")
        items_new = 0
        items_changed = 0
        items_unchanged = 0
        items_failed = 0

        try:
            endpoint = "/transformers"
            resp = await client.get(endpoint)
            payload_hash = compute_payload_hash(resp.text)
            fetched_at = datetime.now(timezone.utc)
            provenance = Provenance(
                fetched_at=fetched_at,
                source_endpoint=endpoint,
                payload_hash=payload_hash,
            )

            transformers = parse_transformers(resp.text)
            for t in transformers:
                try:
                    res = self.store.upsert_transformer(t, provenance)
                    if res == UpsertResult.NEW:
                        items_new += 1
                    elif res == UpsertResult.CHANGED:
                        items_changed += 1
                    else:
                        items_unchanged += 1
                except Exception as e:
                    logger.error("Failed to upsert transformer %s: %s", t.code, e)
                    items_failed += 1

            status = SyncStatus.PARTIAL_FAILURE if items_failed > 0 else SyncStatus.SUCCESS
            finished_run = self.store.finish_sync_run(
                run_id=run.run_id,
                status=status,
                items_new=items_new,
                items_changed=items_changed,
                items_unchanged=items_unchanged,
                items_failed=items_failed,
            )
            return finished_run or run

        except Exception as e:
            logger.exception("Failed to sync transformers: %s", e)
            finished_run = self.store.finish_sync_run(
                run_id=run.run_id,
                status=SyncStatus.FAILED,
                items_new=items_new,
                items_changed=items_changed,
                items_unchanged=items_unchanged,
                items_failed=items_failed + 1,
                error_message=str(e),
            )
            return finished_run or run

    async def sync_all(
        self,
        client: PortalClient,
        max_pages: int | None = None,
        sync_details: bool = False,
    ) -> SyncRun:
        """Full synchronization: transformers, meters list, and optionally meter details."""
        overall_run = self.store.create_sync_run(resource="all")
        tot_new = 0
        tot_changed = 0
        tot_unchanged = 0
        tot_failed = 0

        # 1. Transformers
        t_run = await self.sync_transformers(client)
        tot_new += t_run.items_new
        tot_changed += t_run.items_changed
        tot_unchanged += t_run.items_unchanged
        tot_failed += t_run.items_failed

        # 2. Meters list
        m_run = await self.sync_meters_list(client, max_pages=max_pages)
        tot_new += m_run.items_new
        tot_changed += m_run.items_changed
        tot_unchanged += m_run.items_unchanged
        tot_failed += m_run.items_failed

        # 3. Meter details (if requested)
        if sync_details:
            all_meters = self.store.list_meters(limit=1000)
            m_ids = [m.meter_id for m in all_meters]
            d_run = await self.sync_meter_details(client, m_ids)
            tot_new += d_run.items_new
            tot_changed += d_run.items_changed
            tot_unchanged += d_run.items_unchanged
            tot_failed += d_run.items_failed

        status = SyncStatus.PARTIAL_FAILURE if tot_failed > 0 else SyncStatus.SUCCESS
        finished_run = self.store.finish_sync_run(
            run_id=overall_run.run_id,
            status=status,
            items_new=tot_new,
            items_changed=tot_changed,
            items_unchanged=tot_unchanged,
            items_failed=tot_failed,
        )
        return finished_run or overall_run
