"""FastAPI /v1 endpoints for meters, consumption, and system health."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from urja.api.deps import (
    get_client,
    get_freshness,
    get_store,
    get_sync_engine,
)
from urja.api.errors import MeterNotFoundError, NodeNotFoundError
from urja.api.v1.schemas import (
    ConsumptionResponse,
    CursorPagination,
    HealthResponse,
    HierarchyNodeListResponse,
    HierarchySubtreeResponse,
    HierarchyTreeResponse,
    MeterDetailResponse,
    MeterListResponse,
    MetersNearbyResponse,
    QualityResponse,
)
from urja.client.portal import PortalClient
from urja.domain.models import InstallationStatus, MeterNearbyItem, PhaseType
from urja.freshness.manager import FreshnessManager, ResourceType, ResponseMeta
from urja.hierarchy.tree import HierarchyGraph, audit_network_quality
from urja.store.repository import Store
from urja.sync.engine import SyncEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get(
    "/meters",
    response_model=MeterListResponse,
    summary="List smart meters with cursor pagination and filtering",
    description=(
        "Retrieves a paginated list of smart meters from the local store. "
        "Supports filtering by distribution transformer code, serial number, "
        "manufacturer, phase type, and installation status. "
        "Includes provenance and cache freshness metadata in the `meta` envelope."
    ),
)
async def list_meters(
    cursor: Annotated[
        str | None,
        Query(
            max_length=128,
            description="Opaque pagination cursor (meter_id to paginate after).",
            examples=["J100020"],
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=100,
            description="Number of meters to return per page (max 100).",
        ),
    ] = 20,
    dt_code: Annotated[
        str | None,
        Query(
            max_length=64,
            description="Filter by Distribution Transformer code.",
            examples=["DT-001"],
        ),
    ] = None,
    serial_no: Annotated[
        str | None,
        Query(
            max_length=64,
            description="Filter by meter serial number.",
            examples=["SE33962"],
        ),
    ] = None,
    make: Annotated[
        str | None,
        Query(
            max_length=64,
            description="Filter by meter manufacturer (e.g. HPL, L&T, Secure).",
        ),
    ] = None,
    phase: Annotated[
        PhaseType | None,
        Query(
            description="Filter by phase type (single or three).",
        ),
    ] = None,
    status: Annotated[
        InstallationStatus | None,
        Query(
            description="Filter by installation status (Installed, Faulty, Decommissioned).",
        ),
    ] = None,
    store: Annotated[Store, Depends(get_store)] = None,
    client: Annotated[PortalClient, Depends(get_client)] = None,
    freshness: Annotated[FreshnessManager, Depends(get_freshness)] = None,
    sync_engine: Annotated[SyncEngine, Depends(get_sync_engine)] = None,
) -> MeterListResponse:
    # 1. Query store
    meters, next_cursor, has_more, fetched_at = store.list_meters_cursor(
        cursor=cursor,
        limit=limit,
        dt_code=dt_code,
        serial_no=serial_no,
        make=make,
        phase_type=phase.value if phase else None,
        installation_status=status.value if status else None,
    )
    total = store.count_meters(dt_code=dt_code)

    # 2. If store has never been populated, attempt single-flight seed sync
    source = "store"
    if total == 0 and not cursor:
        try:
            await freshness.single_flight.do(
                "sync:meters_seed",
                lambda: sync_engine.sync_bulk_export(client),
            )
            meters, next_cursor, has_more, fetched_at = store.list_meters_cursor(
                cursor=cursor,
                limit=limit,
                dt_code=dt_code,
                serial_no=serial_no,
                make=make,
                phase_type=phase.value if phase else None,
                installation_status=status.value if status else None,
            )
            total = store.count_meters(dt_code=dt_code)
            source = "portal"
        except Exception as e:
            logger.warning("Auto-seed meters list sync failed: %s", e)

    effective_fetched_at = fetched_at or datetime.now(timezone.utc)
    meta = freshness.evaluate(
        fetched_at=effective_fetched_at,
        resource_type=ResourceType.METER_LIST,
        source=source,
    )

    return MeterListResponse(
        data=meters,
        pagination=CursorPagination(
            next_cursor=next_cursor,
            has_more=has_more,
            total=total,
        ),
        meta=meta,
    )


@router.get(
    "/meters/nearby",
    response_model=MetersNearbyResponse,
    summary="Spatial meter query with bounding-box prefilter and haversine distance",
    description=(
        "Finds smart meters within radius_km of a GPS coordinate using two-phase querying: "
        "bounding-box bounding filter in SQL followed by exact great-circle haversine calculation. "
        "Excludes Null Island (0,0) and records with missing coordinates."
    ),
)
async def get_meters_nearby(
    lat: Annotated[
        float,
        Query(
            ge=-90.0,
            le=90.0,
            description="Latitude in decimal degrees (-90 to 90).",
            examples=[28.6139],
        ),
    ],
    lon: Annotated[
        float,
        Query(
            ge=-180.0,
            le=180.0,
            description="Longitude in decimal degrees (-180 to 180).",
            examples=[77.2090],
        ),
    ],
    radius_km: Annotated[
        float,
        Query(
            gt=0.0,
            le=500.0,
            description="Search radius in kilometers (default 5.0 km, max 500 km).",
            examples=[5.0],
        ),
    ] = 5.0,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=100,
            description="Maximum number of nearby meters to return (default 20, max 100).",
            examples=[20],
        ),
    ] = 20,
    store: Annotated[Store, Depends(get_store)] = None,
    freshness: Annotated[FreshnessManager, Depends(get_freshness)] = None,
) -> MetersNearbyResponse:
    results = store.find_meters_nearby(lat=lat, lon=lon, radius_km=radius_km, limit=limit)
    items = [MeterNearbyItem(meter=m, distance_km=d) for m, d in results]

    meta = freshness.evaluate(
        fetched_at=datetime.now(timezone.utc),
        resource_type=ResourceType.METER_LIST,
        source="store",
    )
    return MetersNearbyResponse(data=items, meta=meta)


@router.get(
    "/meters/{meter_id}",
    response_model=MeterDetailResponse,
    summary="Get detailed meter information",
    description=(
        "Retrieves complete details for a single smart meter, including nameplate attributes, "
        "GPS coordinates, and the reconstructed 7-level network hierarchy breadcrumb. "
        "If data in the local store is missing or stale, triggers a single-flight refresh from the portal. "
        "If portal refresh fails, serves stale store data (stale-while-revalidate)."
    ),
)
async def get_meter_detail(
    meter_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9_\-\.]+$",
            description="Smart meter unique identifier",
            examples=["J100000"],
        ),
    ],
    store: Annotated[Store, Depends(get_store)],
    client: Annotated[PortalClient, Depends(get_client)],
    freshness: Annotated[FreshnessManager, Depends(get_freshness)],
    sync_engine: Annotated[SyncEngine, Depends(get_sync_engine)],
) -> MeterDetailResponse:
    mid = meter_id.strip().upper()
    existing = store.get_meter_with_provenance(mid)

    if existing is not None:
        meter, fetched_at = existing
        meta = freshness.evaluate(
            fetched_at=fetched_at,
            resource_type=ResourceType.METER_DETAIL,
            source="store",
        )
        if not meta.stale:
            return MeterDetailResponse(data=meter, meta=meta)

        # Stale: perform single-flight refresh
        try:
            await freshness.single_flight.do(
                f"sync:meter:{mid}",
                lambda: sync_engine.sync_meter_detail(client, mid),
            )
            refreshed = store.get_meter_with_provenance(mid)
            if refreshed is not None:
                r_meter, r_ts = refreshed
                return MeterDetailResponse(
                    data=r_meter,
                    meta=freshness.evaluate(
                        fetched_at=r_ts,
                        resource_type=ResourceType.METER_DETAIL,
                        source="portal",
                    ),
                )
        except Exception as e:
            logger.warning(
                "Background revalidation failed for %s: %s; serving stale store data", mid, e
            )
            return MeterDetailResponse(data=meter, meta=meta)

    # Not in store: fetch from portal via single-flight
    try:
        await freshness.single_flight.do(
            f"sync:meter:{mid}",
            lambda: sync_engine.sync_meter_detail(client, mid),
        )
    except MeterNotFoundError:
        raise
    except Exception as e:
        logger.error("Failed to fetch meter %s from portal: %s", mid, e)
        raise

    stored = store.get_meter_with_provenance(mid)
    if stored is None:
        raise MeterNotFoundError(mid)

    meter, fetched_at = stored
    return MeterDetailResponse(
        data=meter,
        meta=freshness.evaluate(
            fetched_at=fetched_at,
            resource_type=ResourceType.METER_DETAIL,
            source="portal",
        ),
    )


@router.get(
    "/meters/{meter_id}/consumption",
    response_model=ConsumptionResponse,
    summary="Get meter consumption time series",
    description=(
        "Retrieves 30-minute interval consumption readings (kWh, kVAh, voltage) for a meter. "
        "Supports date filtering via `from` and `to` query parameters. "
        "Applies freshness rules: recent readings (<24h old) have a 5-minute TTL; "
        "historical readings older than 24 hours are treated as immutable."
    ),
)
async def get_meter_consumption(
    meter_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9_\-\.]+$",
            description="Smart meter unique identifier",
            examples=["J100000"],
        ),
    ],
    from_time: Annotated[
        datetime | None,
        Query(
            alias="from",
            description="Filter readings starting from this timestamp (inclusive ISO-8601).",
        ),
    ] = None,
    to_time: Annotated[
        datetime | None,
        Query(
            alias="to",
            description="Filter readings up to this timestamp (inclusive ISO-8601).",
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=500,
            description="Maximum number of reading entries to return.",
        ),
    ] = 100,
    store: Annotated[Store, Depends(get_store)] = None,
    client: Annotated[PortalClient, Depends(get_client)] = None,
    freshness: Annotated[FreshnessManager, Depends(get_freshness)] = None,
    sync_engine: Annotated[SyncEngine, Depends(get_sync_engine)] = None,
) -> ConsumptionResponse:
    if from_time and to_time and from_time > to_time:
        raise HTTPException(
            status_code=400,
            detail="Query parameter 'from' must be earlier than or equal to 'to'.",
        )

    mid = meter_id.strip().upper()
    readings, fetched_at = store.get_readings_range(
        meter_id=mid,
        since=from_time,
        until=to_time,
        limit=limit,
    )

    source = "store"
    # If no readings found or meter missing from store, trigger single-flight detail sync
    if not readings:
        meter = store.get_meter(mid)
        if meter is None:
            # Check if meter exists on portal
            try:
                await freshness.single_flight.do(
                    f"sync:meter:{mid}",
                    lambda: sync_engine.sync_meter_detail(client, mid),
                )
                source = "portal"
            except MeterNotFoundError:
                raise
            except Exception as e:
                logger.error("Could not sync meter %s: %s", mid, e)
                raise

            readings, fetched_at = store.get_readings_range(
                meter_id=mid,
                since=from_time,
                until=to_time,
                limit=limit,
            )

    # Classify freshness policy: recent vs historical
    now = datetime.now(timezone.utc)
    is_recent = False
    if readings:
        newest_ts = readings[0].timestamp
        is_recent = (now - newest_ts).total_seconds() < 86400  # within 24 hours

    res_type = (
        ResourceType.CONSUMPTION_RECENT if is_recent else ResourceType.CONSUMPTION_HISTORICAL
    )

    meta = freshness.evaluate(
        fetched_at=fetched_at or now,
        resource_type=res_type,
        source=source,
    )

    return ConsumptionResponse(data=readings, meta=meta)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness check, portal connectivity, and sync status",
    description=(
        "Reports the operational health of the API service, upstream connectivity "
        "to the Urja Meter Ops legacy portal, and details of the most recent data sync run."
    ),
)
async def health(
    store: Annotated[Store, Depends(get_store)],
    client: Annotated[PortalClient, Depends(get_client)],
    freshness: Annotated[FreshnessManager, Depends(get_freshness)],
) -> HealthResponse:
    portal_reachable = await client.ping(timeout=3.0)

    last_sync = store.get_latest_sync_run()
    status = "ok" if portal_reachable else "degraded"

    meta = ResponseMeta(
        fetched_at=datetime.now(timezone.utc),
        age_seconds=0.0,
        stale=not portal_reachable,
        source="store",
    )

    return HealthResponse(
        status=status,
        portal_reachable=portal_reachable,
        last_sync=last_sync,
        meta=meta,
    )


# ── Network Hierarchy & Quality Endpoints ─────────────────────────────


@router.get(
    "/hierarchy",
    response_model=HierarchyTreeResponse,
    summary="Reconstructed 7-level network tree",
    description=(
        "Synthesizes the complete utility network hierarchy tree (Zone › Circle › "
        "Division › Subdivision › Substation › Feeder › DT) with deterministic repair rules: "
        "ID normalization, multiple parent tie-breaking, cycle breaking, and dangling ref detachment. "
        "No nodes are ever deleted."
    ),
)
async def get_hierarchy(
    store: Annotated[Store, Depends(get_store)],
    freshness: Annotated[FreshnessManager, Depends(get_freshness)],
) -> HierarchyTreeResponse:
    nodes = store.list_all_nodes()
    meters = store.list_all_meters()
    graph = HierarchyGraph(nodes, meters)
    tree, _ = graph.analyze_and_build()

    meta = freshness.evaluate(
        fetched_at=datetime.now(timezone.utc),
        resource_type=ResourceType.TRANSFORMERS,
        source="store",
    )
    return HierarchyTreeResponse(data=tree, meta=meta)


@router.get(
    "/nodes/{node_id}/children",
    response_model=HierarchyNodeListResponse,
    summary="Direct children of a hierarchy node",
    description="Returns the immediate children of the specified hierarchy node path or code.",
)
async def get_node_children(
    node_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=256,
            pattern=r"^[A-Za-z0-9_\-\.\/ %]+$",
            description="Hierarchy node unique path or code",
            examples=["ZONE-NORTH"],
        ),
    ],
    store: Annotated[Store, Depends(get_store)],
    freshness: Annotated[FreshnessManager, Depends(get_freshness)],
) -> HierarchyNodeListResponse:
    node = store.get_hierarchy_node(node_id)
    if not node:
        raise NodeNotFoundError(node_id)

    children = store.get_node_children(node_id)
    meta = freshness.evaluate(
        fetched_at=datetime.now(timezone.utc),
        resource_type=ResourceType.TRANSFORMERS,
        source="store",
    )
    return HierarchyNodeListResponse(data=children, meta=meta)


@router.get(
    "/nodes/{node_id}/subtree",
    response_model=HierarchySubtreeResponse,
    summary="Subtree query using recursive CTE",
    description=(
        "Fetches all descendants of the specified hierarchy node using a recursive "
        "Common Table Expression (CTE) with depth guard."
    ),
)
async def get_node_subtree(
    node_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=256,
            pattern=r"^[A-Za-z0-9_\-\.\/ %]+$",
            description="Hierarchy node unique path or code",
            examples=["ZONE-NORTH"],
        ),
    ],
    store: Annotated[Store, Depends(get_store)],
    freshness: Annotated[FreshnessManager, Depends(get_freshness)],
) -> HierarchySubtreeResponse:
    node = store.get_hierarchy_node(node_id)
    if not node:
        raise NodeNotFoundError(node_id)

    subtree_nodes = store.get_node_subtree_cte(node_id)
    meta = freshness.evaluate(
        fetched_at=datetime.now(timezone.utc),
        resource_type=ResourceType.TRANSFORMERS,
        source="store",
    )
    return HierarchySubtreeResponse(data=subtree_nodes, meta=meta)


@router.get(
    "/quality",
    response_model=QualityResponse,
    summary="Network data quality defect report",
    description=(
        "Audits the network for data anomalies across hierarchy (orphans, cycles, "
        "multiple parents, dangling references, ID mismatches), meters (null island, "
        "missing nameplates), and consumption readings (monotonicity violations)."
    ),
)
async def get_quality(
    store: Annotated[Store, Depends(get_store)],
    freshness: Annotated[FreshnessManager, Depends(get_freshness)],
) -> QualityResponse:
    nodes = store.list_all_nodes()
    meters = store.list_all_meters()
    report = audit_network_quality(nodes, meters)

    meta = freshness.evaluate(
        fetched_at=datetime.now(timezone.utc),
        resource_type=ResourceType.TRANSFORMERS,
        source="store",
    )
    return QualityResponse(data=report, meta=meta)
