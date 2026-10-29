"""Synchronization engine — orchestrates portal scraping, adapter parsing, and store ingestion.

Supports:
- Verified bulk export sync via `/portal/export` (signed with dynamic HMAC-SHA256 from `/portal/keys`)
- Paginated search sync via `/portal/meters/search`
- Energy telemetry sync via `/portal/meters/{id}/energy` (bounded concurrency, default 4)
- Atomic SQLite transactions (`with self.store.transaction(): ...`)
- Idempotent upserts with change detection (NEW, CHANGED, UNCHANGED)
- Audit logging via ``sync_runs`` table
- Zero-deletions invariant: missing records are never dropped
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from urja.adapters import (
    parse_dts_payload,
    parse_energy_payload,
    parse_export_payload,
    parse_search_payload,
)
from urja.client.exceptions import (
    MeterNotFoundError,
    PortalError,
    PortalShapeError,
)
from urja.client.portal import PortalClient
from urja.domain.models import (
    Location,
    Meter,
    Provenance,
    Reading,
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

    # ── Bulk Export Sync ──────────────────────────────────────────────

    async def sync_bulk_export(
        self,
        client: PortalClient,
    ) -> SyncRun:
        """Fetch and ingest full network metadata from GET /portal/export?page=1.

        Uses ephemeral HMAC-SHA256 signing via client.get_export().
        Executes in an atomic SQLite transaction — if parsing or database insertion fails,
        the entire batch is rolled back, leaving existing data untouched.
        """
        run = self.store.create_sync_run(resource="bulk_export")
        items_new = 0
        items_changed = 0
        items_unchanged = 0
        items_failed = 0

        endpoint = "/portal/export?page=1"
        try:
            resp = await client.get_export(page=1)
            payload_hash = compute_payload_hash(resp.text)
            fetched_at = datetime.now(timezone.utc)
            provenance = Provenance(
                fetched_at=fetched_at,
                source_endpoint=endpoint,
                payload_hash=payload_hash,
            )

            data: dict[str, Any] = resp.json()
            meters, hierarchy_nodes, _total = parse_export_payload(data)

            # Atomic transaction for the entire export ingestion
            with self.store.transaction():
                for m in meters:
                    res = self.store.upsert_meter(m, provenance)
                    if res == UpsertResult.NEW:
                        items_new += 1
                    elif res == UpsertResult.CHANGED:
                        items_changed += 1
                    else:
                        items_unchanged += 1

                for n in hierarchy_nodes:
                    self.store.upsert_hierarchy_node(n, provenance)

            finished_run = self.store.finish_sync_run(
                run_id=run.run_id,
                status=SyncStatus.SUCCESS,
                items_new=items_new,
                items_changed=items_changed,
                items_unchanged=items_unchanged,
                items_failed=items_failed,
            )
            return finished_run or run

        except Exception as e:
            logger.exception("Bulk export synchronization failed: %s", e)
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

    # ── Energy Telemetry Sync ─────────────────────────────────────────

    async def sync_meter_energy(
        self,
        client: PortalClient,
        meter_id: str,
    ) -> tuple[int, int, int]:
        """Fetch and ingest energy readings for a single meter.

        Runs in an isolated per-meter transaction.
        Returns (readings_new, readings_changed, readings_unchanged).
        """
        endpoint = f"/portal/meters/{meter_id}/energy"
        resp = await client.get(endpoint)
        if resp.status_code == 404:
            raise MeterNotFoundError(meter_id)
        if resp.status_code >= 400:
            raise PortalError(f"HTTP {resp.status_code} fetching energy for {meter_id}")

        payload_hash = compute_payload_hash(resp.text)
        fetched_at = datetime.now(timezone.utc)
        provenance = Provenance(
            fetched_at=fetched_at,
            source_endpoint=endpoint,
            payload_hash=payload_hash,
        )

        readings: list[Reading] = parse_energy_payload(resp.json(), meter_id=meter_id)
        new_cnt = 0
        changed_cnt = 0
        unchanged_cnt = 0

        with self.store.transaction():
            for r in readings:
                res = self.store.upsert_reading(r, provenance)
                if res == UpsertResult.NEW:
                    new_cnt += 1
                elif res == UpsertResult.CHANGED:
                    changed_cnt += 1
                else:
                    unchanged_cnt += 1

        return new_cnt, changed_cnt, unchanged_cnt

    async def sync_meters_energy(
        self,
        client: PortalClient,
        meter_ids: list[str] | None = None,
        concurrency: int = 4,
    ) -> SyncRun:
        """Batch synchronize energy readings for meters with bounded concurrency.

        Args:
            client: Authenticated portal client.
            meter_ids: Specific list of meter IDs, or None to query all known meters.
            concurrency: Maximum parallel requests to legacy portal (default: 4).
        """
        run = self.store.create_sync_run(resource="meter_energy")
        items_new = 0
        items_changed = 0
        items_unchanged = 0
        items_failed = 0

        if meter_ids is None:
            all_meters = self.store.list_all_meters()
            meter_ids = [m.meter_id for m in all_meters]

        sem = asyncio.Semaphore(concurrency)

        async def _worker(mid: str) -> tuple[int, int, int] | None:
            async with sem:
                try:
                    return await self.sync_meter_energy(client, mid)
                except Exception as e:
                    logger.error("Failed to sync energy for meter %s: %s", mid, e)
                    return None

        results = await asyncio.gather(*[_worker(mid) for mid in meter_ids])

        for res in results:
            if res is None:
                items_failed += 1
            else:
                n, c, u = res
                items_new += n
                items_changed += c
                items_unchanged += u

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

    # ── Search & Paginated Sync ───────────────────────────────────────

    async def sync_meters_search(
        self,
        client: PortalClient,
        max_pages: int | None = None,
    ) -> SyncRun:
        """Synchronize meters from paginated GET /portal/meters/search?q=&page=N."""
        run = self.store.create_sync_run(resource="meters_search")
        items_new = 0
        items_changed = 0
        items_unchanged = 0
        items_failed = 0
        current_page = 1
        page_size = 20

        try:
            while True:
                if max_pages is not None and current_page > max_pages:
                    break

                endpoint = f"/portal/meters/search?q=&page={current_page}"
                try:
                    resp = await client.get(endpoint)
                    payload_hash = compute_payload_hash(resp.text)
                    fetched_at = datetime.now(timezone.utc)
                    provenance = Provenance(
                        fetched_at=fetched_at,
                        source_endpoint=endpoint,
                        payload_hash=payload_hash,
                    )

                    meters, total, _page, page_size = parse_search_payload(resp.json())
                    if not meters:
                        break

                    with self.store.transaction():
                        for meter in meters:
                            res = self.store.upsert_meter(meter, provenance)
                            if res == UpsertResult.NEW:
                                items_new += 1
                            elif res == UpsertResult.CHANGED:
                                items_changed += 1
                            else:
                                items_unchanged += 1

                    if current_page * page_size >= total:
                        break
                    current_page += 1

                except Exception as e:
                    logger.error("Failed to sync search page %d: %s", current_page, e)
                    items_failed += 1
                    break

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
            logger.exception("Critical error during search sync: %s", e)
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

    # ── Single-Meter On-Demand Sync ──────────────────────────────────

    async def sync_meter_detail(
        self,
        client: PortalClient,
        meter_id: str,
    ) -> tuple[UpsertResult, int, int]:
        """Fetch and ingest detail for a single meter using verified JSON endpoints.

        Coordinates:
        - GET /portal/meters/search?q={mid} for nameplate specs
        - GET /portal/meters/{mid}/geo for coordinates
        - GET /portal/meters/{mid}/energy for consumption time-series
        """
        mid = meter_id.strip().upper()
        search_endpoint = f"/portal/meters/search?q={mid}"
        s_resp = await client.get(search_endpoint)
        if s_resp.status_code >= 400:
            raise PortalError(f"HTTP {s_resp.status_code} searching for meter {mid}")

        try:
            meters, _, _, _ = parse_search_payload(s_resp.json())
        except Exception as e:
            raise PortalShapeError(f"Failed to parse search payload for {mid}: {e}") from e

        matching_meter: Meter | None = None
        for m in meters:
            if m.meter_id.upper() == mid:
                matching_meter = m
                break

        if matching_meter is None:
            existing = self.store.get_meter(mid)
            if existing is None:
                raise MeterNotFoundError(mid)
            matching_meter = existing

        # Fetch richer nameplate and hierarchy from /meters/{mid}/__data.json
        try:
            detail_resp = await client.get(f"/meters/{mid}/__data.json")
            if detail_resp.status_code == 200:
                payload = detail_resp.json()
                nodes = payload.get("nodes", [])
                if len(nodes) >= 3 and nodes[2] and nodes[2].get("type") == "data":
                    data_list = nodes[2].get("data", [])
                    if data_list and isinstance(data_list[0], dict):
                        def resolve(idx: Any) -> Any:
                            if isinstance(idx, int) and 0 <= idx < len(data_list):
                                return data_list[idx]
                            return idx

                        root = data_list[0]
                        detail_ref = resolve(root.get("detail"))
                        if isinstance(detail_ref, dict) and "data" in detail_ref:
                            item_indices = resolve(detail_ref["data"])
                            if isinstance(item_indices, list):
                                for i in item_indices:
                                    item_obj = resolve(i)
                                    if isinstance(item_obj, dict):
                                        p_name = resolve(item_obj.get("parameterName"))
                                        p_val = resolve(item_obj.get("parameterValue"))
                                        if p_name == "Installation Type" and p_val:
                                            matching_meter.installation_type = str(p_val)

                        hier_ref = resolve(root.get("hierarchy"))
                        if isinstance(hier_ref, dict):
                            hier_dict = {k: resolve(v) for k, v in hier_ref.items()}
                            matching_meter.raw_hierarchy = hier_dict
                elif len(nodes) >= 3 and nodes[2] and nodes[2].get("status") == 404:
                    raise MeterNotFoundError(mid)
        except MeterNotFoundError:
            raise
        except Exception as e:
            logger.debug("SvelteKit detail fetch skipped for %s: %s", mid, e)

        # Fetch coordinates from /portal/meters/{mid}/geo if not present
        if matching_meter.location is None:
            try:
                geo_resp = await client.get(f"/portal/meters/{mid}/geo")
                if geo_resp.status_code == 200:
                    geo_data = geo_resp.json()
                    if isinstance(geo_data, dict) and "data" in geo_data:
                        coords = geo_data["data"]
                        lat_val = coords.get("latitude")
                        lng_val = coords.get("longitude")
                        if lat_val is not None and lng_val is not None:
                            try:
                                lat = float(lat_val)
                                lng = float(lng_val)
                                q_flags = []
                                if lat == 0.0 and lng == 0.0:
                                    q_flags.append("null_island")
                                matching_meter.location = Location(
                                    latitude=lat,
                                    longitude=lng,
                                    quality_flags=q_flags,
                                )
                            except (ValueError, TypeError):
                                pass
            except Exception as e:
                logger.debug("Geo fetch skipped for %s: %s", mid, e)

        # Fetch energy telemetry from /portal/meters/{mid}/energy
        readings: list[Reading] = []
        try:
            energy_resp = await client.get(f"/portal/meters/{mid}/energy")
            if energy_resp.status_code == 200:
                readings = parse_energy_payload(energy_resp.json(), meter_id=mid)
        except Exception as e:
            logger.debug("Energy fetch skipped for %s: %s", mid, e)

        payload_hash = compute_payload_hash(s_resp.text)
        provenance = Provenance(
            fetched_at=datetime.now(timezone.utc),
            source_endpoint=search_endpoint,
            payload_hash=payload_hash,
        )

        with self.store.transaction():
            meter_res = self.store.upsert_meter(matching_meter, provenance)
            for reading in readings:
                self.store.upsert_reading(reading, provenance)

        return meter_res, len(readings), 0

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

    # ── Transformers Ingestion ───────────────────────────────────────

    async def sync_transformers(
        self,
        client: PortalClient,
        max_pages: int | None = None,
    ) -> SyncRun:
        """Synchronize transformers from paginated JSON endpoint /portal/dts?page=N."""
        run = self.store.create_sync_run(resource="transformers")
        items_new = 0
        items_changed = 0
        items_unchanged = 0
        items_failed = 0
        current_page = 1
        page_size = 20

        try:
            while True:
                if max_pages is not None and current_page > max_pages:
                    break

                endpoint = f"/portal/dts?page={current_page}"
                resp = await client.get(endpoint)
                if resp.status_code != 200:
                    if resp.status_code != 404:
                        items_failed += 1
                    break

                payload_hash = compute_payload_hash(resp.text)
                provenance = Provenance(
                    fetched_at=datetime.now(timezone.utc),
                    source_endpoint=endpoint,
                    payload_hash=payload_hash,
                )

                transformers, total, _page, page_size = parse_dts_payload(resp.json())
                if not transformers:
                    break

                with self.store.transaction():
                    for t in transformers:
                        res = self.store.upsert_transformer(t, provenance)
                        if res == UpsertResult.NEW:
                            items_new += 1
                        elif res == UpsertResult.CHANGED:
                            items_changed += 1
                        else:
                            items_unchanged += 1

                if current_page * page_size >= total:
                    break
                current_page += 1

        except Exception as e:
            logger.error("Failed to sync transformers: %s", e)
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

    # ── Master Orchestration ─────────────────────────────────────────

    async def sync_all(
        self,
        client: PortalClient,
        max_pages: int | None = None,
        sync_energy: bool = True,
        energy_concurrency: int = 4,
        energy_limit: int | None = None,
    ) -> SyncRun:
        """Full synchronization prioritizing bulk export, transformers, and energy telemetry."""
        overall_run = self.store.create_sync_run(resource="all")
        tot_new = 0
        tot_changed = 0
        tot_unchanged = 0
        tot_failed = 0

        # 1. Attempt Bulk Export
        export_success = False
        try:
            export_run = await self.sync_bulk_export(client)
            tot_new += export_run.items_new
            tot_changed += export_run.items_changed
            tot_unchanged += export_run.items_unchanged
            tot_failed += export_run.items_failed
            if export_run.status in (SyncStatus.SUCCESS, SyncStatus.PARTIAL_FAILURE):
                export_success = True
        except Exception as e:
            logger.warning("Bulk export failed, falling back to search: %s", e)

        # 2. Fallback to Search if Bulk Export failed
        if not export_success:
            m_run = await self.sync_meters_search(client, max_pages=max_pages)
            tot_new += m_run.items_new
            tot_changed += m_run.items_changed
            tot_unchanged += m_run.items_unchanged
            tot_failed += m_run.items_failed

        # 3. Synchronize Transformers (/portal/dts?page=N)
        try:
            t_run = await self.sync_transformers(client)
            tot_new += t_run.items_new
            tot_changed += t_run.items_changed
            tot_unchanged += t_run.items_unchanged
            tot_failed += t_run.items_failed
        except Exception as e:
            logger.warning("Transformers sync failed: %s", e)

        # 4. Synchronize Energy Telemetry
        if sync_energy:
            all_meters = self.store.list_all_meters()
            meter_ids = [m.meter_id for m in all_meters]
            if energy_limit is not None:
                meter_ids = meter_ids[:energy_limit]

            if meter_ids:
                e_run = await self.sync_meters_energy(
                    client, meter_ids=meter_ids, concurrency=energy_concurrency
                )
                tot_new += e_run.items_new
                tot_changed += e_run.items_changed
                tot_unchanged += e_run.items_unchanged
                tot_failed += e_run.items_failed

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
