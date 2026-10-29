"""API response schemas with freshness metadata and pagination."""

from __future__ import annotations

from pydantic import BaseModel, Field

from urja.domain.models import (
    HierarchyNode,
    Meter,
    MeterNearbyItem,
    QualityReport,
    Reading,
    SyncRun,
    TreeNode,
)
from urja.freshness.manager import ResponseMeta


class CursorPagination(BaseModel):
    """Cursor-based pagination envelope."""

    next_cursor: str | None = Field(
        None, description="Opaque cursor token for fetching the next page."
    )
    has_more: bool = Field(
        ..., description="True if additional pages are available beyond this result set."
    )
    total: int = Field(
        ..., description="Total number of records matching query filters in the store."
    )


class MeterListResponse(BaseModel):
    """Response payload for GET /v1/meters."""

    data: list[Meter] = Field(..., description="List of smart meters.")
    pagination: CursorPagination = Field(..., description="Cursor pagination details.")
    meta: ResponseMeta = Field(..., description="Cache freshness and origin metadata.")


class MeterDetailResponse(BaseModel):
    """Response payload for GET /v1/meters/{id}."""

    data: Meter = Field(..., description="Full smart meter detail.")
    meta: ResponseMeta = Field(..., description="Cache freshness and origin metadata.")


class ConsumptionResponse(BaseModel):
    """Response payload for GET /v1/meters/{id}/consumption."""

    data: list[Reading] = Field(
        ..., description="Time series consumption readings ordered descending by timestamp."
    )
    meta: ResponseMeta = Field(..., description="Cache freshness and origin metadata.")


class HealthResponse(BaseModel):
    """System health and upstream portal status."""

    status: str = Field(..., description="Service health: 'ok' or 'degraded'.")
    portal_reachable: bool = Field(..., description="True if the legacy portal is accessible.")
    last_sync: SyncRun | None = Field(
        None, description="Details of the most recent synchronization job."
    )
    meta: ResponseMeta = Field(..., description="Freshness metadata for the health evaluation.")


class HierarchyTreeResponse(BaseModel):
    """Response payload for GET /v1/hierarchy."""

    data: list[TreeNode] = Field(
        ..., description="Reconstructed 7-level network tree with deterministic repairs."
    )
    meta: ResponseMeta = Field(..., description="Cache freshness and origin metadata.")


class HierarchyNodeListResponse(BaseModel):
    """Response payload for GET /v1/nodes/{id}/children."""

    data: list[HierarchyNode] = Field(
        ..., description="Direct child nodes of the requested hierarchy node."
    )
    meta: ResponseMeta = Field(..., description="Cache freshness and origin metadata.")


class HierarchySubtreeResponse(BaseModel):
    """Response payload for GET /v1/nodes/{id}/subtree."""

    data: list[HierarchyNode] = Field(
        ..., description="Subtree of descendants evaluated via recursive CTE with depth guard."
    )
    meta: ResponseMeta = Field(..., description="Cache freshness and origin metadata.")


class QualityResponse(BaseModel):
    """Response payload for GET /v1/quality."""

    data: QualityReport = Field(
        ..., description="Aggregated quality defects across hierarchy, meters, and readings."
    )
    meta: ResponseMeta = Field(..., description="Cache freshness and origin metadata.")


class MetersNearbyResponse(BaseModel):
    """Response payload for GET /v1/meters/nearby."""

    data: list[MeterNearbyItem] = Field(
        ..., description="Meters within query radius sorted by great-circle distance."
    )
    meta: ResponseMeta = Field(..., description="Cache freshness and origin metadata.")
