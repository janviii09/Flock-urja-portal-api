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

from pydantic import BaseModel, Field

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
    """A single node in the network hierarchy."""

    code: str  # e.g. "Z-01", "C-01", "DT-001"
    name: str  # e.g. "Jaipur Zone 1", "Circle 1"
    level: HierarchyLevel
    parent_code: str | None = None
    quality_flags: list[str] = Field(default_factory=list)


class HierarchyPath(BaseModel):
    """Full path from zone down to DT for a meter, as shown in the breadcrumb."""

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
    """GPS coordinates from the meter detail page.

    Portal displays as: "26.938961002479868, 75.83095696146852"
    with label "Latitude / Longitude".
    """

    latitude: float
    longitude: float
    quality_flags: list[str] = Field(default_factory=list)


class Meter(BaseModel):
    """A smart meter as shown on the portal.

    List page columns: METER, SERIAL, MAKE, PHASE, STATUS, DT
    Detail page adds: Installation Type, Location, Consumption, hierarchy breadcrumb.
    """

    meter_id: str  # e.g. "J100000"
    serial_no: str  # e.g. "SE33962"
    make: str  # e.g. "HPL", "L&T", "Genus", "Allied", "Secure"
    phase_type: PhaseType | None = None
    installation_status: InstallationStatus | None = None
    installation_type: str | None = None  # e.g. "Whole Current"
    dt_code: str | None = None  # e.g. "DT-001" — from list page
    location: Location | None = None
    hierarchy_path: HierarchyPath | None = None
    quality_flags: list[str] = Field(default_factory=list)


# ── Consumption reading ──────────────────────────────────────────────


class Reading(BaseModel):
    """A single consumption reading row from the meter detail page.

    Columns: TIMESTAMP, KWH, KVAH, V(R)
    Timestamps look like: "23/06/2026 23:30" (DD/MM/YYYY HH:MM).
    KWH/KVAH are cumulative floats; V(R) is voltage (integer-ish).
    """

    meter_id: str
    timestamp: datetime
    kwh: float
    kvah: float
    voltage_r: float | None = None  # V(R) column
    quality_flags: list[str] = Field(default_factory=list)


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
