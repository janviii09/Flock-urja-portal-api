"""Domain models — normalised Pydantic v2 schemas.

These represent the *clean* shapes that flow through our system.
Messy portal data is never silently fixed; instead, issues are recorded in
``quality_flags`` on each model so consumers can decide what to trust.

Observed portal data (from direct exploration):
- Meter list columns: METER, SERIAL, MAKE, PHASE, STATUS, DT
- Meter detail sections: Nameplate, Location, Consumption, hierarchy breadcrumb
- Transformer columns: CODE, NAME, FEEDER, CAPACITY (KVA)
- Hierarchy levels: Zone > Circle > Division > Subdivision > Substation > Feeder > DT
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

# ── Enums ─────────────────────────────────────────────────────────────


class InstallationStatus(str, Enum):
    """Meter status values observed on the portal."""

    INSTALLED = "Installed"
    FAULTY = "Faulty"
    DECOMMISSIONED = "Decommissioned"


class PhaseType(str, Enum):
    """Phase types observed on the portal."""

    SINGLE = "single"
    THREE = "three"


# ── Hierarchy ─────────────────────────────────────────────────────────


class HierarchyLevel(str, Enum):
    """Network hierarchy levels, top to bottom.

    Observed breadcrumb pattern on meter detail page:
    Zone > Circle > Division > Subdivision > Substation > Feeder > DT
    """

    ZONE = "zone"
    CIRCLE = "circle"
    DIVISION = "division"
    SUBDIVISION = "subdivision"
    SUBSTATION = "substation"
    FEEDER = "feeder"
    DT = "dt"


class HierarchyNode(BaseModel):
    """A single node in the network hierarchy keyed by its canonical full path."""

    node_path: str = Field(
        default="", description="Canonical path from root to node (e.g. Z-01/C-01/D-01/SD-01)."
    )
    code: str | None = None  # e.g. "Z-01", "C-01", "DT-001" (may be empty on dirty data)
    name: str | None = None  # e.g. "Jaipur Zone 1", "Circle 1"
    level: HierarchyLevel
    parent_path: str | None = None  # Canonical node_path of parent node
    parent_code: str | None = None  # Backward-compatible alias/fallback
    quality_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _populate_paths_and_codes(cls, data: Any) -> Any:
        if isinstance(data, dict):
            code = data.get("code")
            parent_code = data.get("parent_code")
            if not data.get("node_path") and code:
                data["node_path"] = str(code).strip()
            if not data.get("parent_path") and parent_code:
                data["parent_path"] = str(parent_code).strip()
            if not data.get("parent_code") and data.get("parent_path"):
                data["parent_code"] = str(data["parent_path"]).split("/")[-1]
            if not data.get("code") and data.get("node_path"):
                data["code"] = str(data["node_path"]).split("/")[-1]
        return data


class HierarchyPath(BaseModel):
    """Full path from zone down to DT for a meter."""

    zone: HierarchyNode | None = None
    circle: HierarchyNode | None = None
    division: HierarchyNode | None = None
    subdivision: HierarchyNode | None = None
    substation: HierarchyNode | None = None
    feeder: HierarchyNode | None = None
    dt: HierarchyNode | None = None
    quality_flags: list[str] = Field(default_factory=list)


# ── Meter ─────────────────────────────────────────────────────────────


class Location(BaseModel):
    """GPS coordinates.

    Numeric floats in export ({lat, lng}); strings in /geo ({latitude, longitude}).
    """

    latitude: float
    longitude: float
    quality_flags: list[str] = Field(default_factory=list)


class Meter(BaseModel):
    """A smart meter with verified portal export metadata and technical specs."""

    meter_id: str  # e.g. "J100000"
    serial_no: str | None = None  # e.g. "SE33962"
    make: str | None = None  # e.g. "HPL", "L&T", "Genus", "Allied", "Secure"
    phase_type: PhaseType | None = None
    installation_status: InstallationStatus | None = None
    installation_type: str | None = None  # installType: "Whole Current", "CT Operated"
    build: str | None = None  # "legacy" or "v2" from export
    dt_code: str | None = None  # e.g. "DT-001"
    location: Location | None = None
    hierarchy_path: list[str] | HierarchyPath | None = None  # Breadcrumb segments or HierarchyPath
    raw_hierarchy: dict[str, Any] | None = None  # Raw export dictionary
    quality_flags: list[str] = Field(default_factory=list)


# ── Consumption reading ──────────────────────────────────────────────


class Reading(BaseModel):
    """A single consumption reading row.

    Preserves raw unzoned portal timestamp string alongside normalized UTC datetime.
    KWH/KVAH are cumulative registers; V(R) is voltage.
    """

    meter_id: str
    raw_timestamp: str = ""  # Literal portal string (e.g. "23/06/2026 23:30")
    timestamp: datetime  # UTC normalized datetime for ordering/querying
    kwh: float
    kvah: float
    voltage_r: float | None = None  # V(R) column
    quality_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _default_raw_timestamp(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("raw_timestamp") and data.get("timestamp"):
            ts = data["timestamp"]
            data["raw_timestamp"] = ts.isoformat() if isinstance(ts, datetime) else str(ts)
        return data


# ── Transformer ──────────────────────────────────────────────────────


class Transformer(BaseModel):
    """A distribution transformer (DT) as shown on /transformers.

    Columns: CODE, NAME, FEEDER, CAPACITY (KVA).
    No individual detail page exists (returns 404).
    """

    code: str  # e.g. "DT-001"
    name: str  # e.g. "Malviya Nagar DT 1"
    feeder: str  # e.g. "F-001"
    capacity_kva: float
    quality_flags: list[str] = Field(default_factory=list)


# ── Provenance (for store layer) ─────────────────────────────────────


class Provenance(BaseModel):
    """Tracks where and when a piece of data was fetched from the portal."""

    fetched_at: datetime
    source_endpoint: str  # e.g. "/meters/J100000"
    payload_hash: str  # SHA-256 hex of raw portal response


# ── Sync & Store Tracking ─────────────────────────────────────────────


class UpsertResult(str, Enum):
    """Outcome of an idempotent upsert with change detection."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class SyncStatus(str, Enum):
    """Status lifecycle of a sync run."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"


class SyncRun(BaseModel):
    """Audit record for a synchronization operation."""

    run_id: str
    started_at: datetime
    completed_at: datetime | None = None
    status: SyncStatus = SyncStatus.RUNNING
    resource: str  # e.g. "meters_list", "meter_detail", "transformers", "all"
    items_new: int = 0
    items_changed: int = 0
    items_unchanged: int = 0
    items_failed: int = 0
    error_message: str | None = None


# ── Hierarchy Tree & Query Models ─────────────────────────────────────


class TreeNode(BaseModel):
    """Recursive node in the reconstructed network tree."""

    node_path: str = Field(default="", description="Canonical path from root to node.")
    code: str | None = None
    name: str | None = None
    level: HierarchyLevel
    parent_path: str | None = None
    parent_code: str | None = None
    quality_flags: list[str] = Field(default_factory=list)
    children: list[TreeNode] = Field(default_factory=list)
    meter_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _populate_paths_and_codes(cls, data: Any) -> Any:
        if isinstance(data, dict):
            code = data.get("code")
            parent_code = data.get("parent_code")
            if not data.get("node_path") and code:
                data["node_path"] = str(code).strip()
            if not data.get("parent_path") and parent_code:
                data["parent_path"] = str(parent_code).strip()
            if not data.get("parent_code") and data.get("parent_path"):
                data["parent_code"] = str(data["parent_path"]).split("/")[-1]
            if not data.get("code") and data.get("node_path"):
                data["code"] = str(data["node_path"]).split("/")[-1]
        return data


class MeterNearbyItem(BaseModel):
    """A meter and its calculated great-circle distance from a query coordinate."""

    meter: Meter
    distance_km: float


class QualityReport(BaseModel):
    """Defect report aggregating data quality issues across the utility network."""

    total_meters_audited: int
    total_nodes_audited: int
    hierarchy_defects: dict[str, Any]
    meter_defects: dict[str, Any]
    consumption_defects: dict[str, Any]
    quality_score: float  # Percentage of clean, defect-free records (0.0 to 100.0)


TreeNode.model_rebuild()
QualityReport.model_rebuild()
