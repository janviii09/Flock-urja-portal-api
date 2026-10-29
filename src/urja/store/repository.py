"""SQLite repository interface for Urja Meter Ops.

Implements idempotent upserts with change detection, provenance tracking,
and query methods for meters, consumption readings, hierarchy nodes,
transformers, and sync audit runs.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from urja.domain.models import (
    HierarchyLevel,
    HierarchyNode,
    HierarchyPath,
    InstallationStatus,
    Location,
    Meter,
    PhaseType,
    Provenance,
    Reading,
    SyncRun,
    SyncStatus,
    Transformer,
    UpsertResult,
)
from urja.store.schema import SCHEMA_SQL

logger = logging.getLogger(__name__)


def _dt_to_iso(dt: datetime) -> str:
    """Format datetime as UTC ISO-8601 string."""
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.isoformat()


def _iso_to_dt(iso_str: str) -> datetime:
    """Parse ISO-8601 string into UTC datetime."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Store:
    """SQLite repository for storing and querying Urja entity data.

    Supports file-based or in-memory (``:memory:``) databases.
    All write transactions are atomic and idempotent.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,  # Manual transaction management (autocommit mode)
        )
        self._conn.row_factory = sqlite3.Row
        self.init_db()

    def init_db(self) -> None:
        """Initialize database schema, foreign keys, and indexes."""
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()

    # ── Meters ────────────────────────────────────────────────────────

    def upsert_meter(self, meter: Meter, provenance: Provenance) -> UpsertResult:
        """Idempotently insert or update a meter with change detection."""
        fetched_at_str = _dt_to_iso(provenance.fetched_at)
        quality_flags_json = json.dumps(meter.quality_flags)
        hierarchy_json = meter.hierarchy_path.model_dump_json() if meter.hierarchy_path else None
        lat = meter.location.latitude if meter.location else None
        lon = meter.location.longitude if meter.location else None
        phase = meter.phase_type.value if meter.phase_type else None
        status = meter.installation_status.value if meter.installation_status else None

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT serial_no, make, phase_type, installation_status, "
                "installation_type, dt_code, latitude, longitude, payload_hash "
                "FROM meters WHERE meter_id = ?",
                (meter.meter_id,),
            )
            existing = cursor.fetchone()

            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO meters (
                        meter_id, serial_no, make, phase_type, installation_status,
                        installation_type, dt_code, latitude, longitude,
                        hierarchy_json, quality_flags, fetched_at,
                        source_endpoint, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        meter.meter_id,
                        meter.serial_no,
                        meter.make,
                        phase,
                        status,
                        meter.installation_type,
                        meter.dt_code,
                        lat,
                        lon,
                        hierarchy_json,
                        quality_flags_json,
                        fetched_at_str,
                        provenance.source_endpoint,
                        provenance.payload_hash,
                    ),
                )
                return UpsertResult.NEW

            # Change detection: check if entity data fields changed
            unchanged = (
                existing["serial_no"] == meter.serial_no
                and existing["make"] == meter.make
                and existing["phase_type"] == phase
                and existing["installation_status"] == status
                and existing["installation_type"] == meter.installation_type
                and existing["dt_code"] == meter.dt_code
                and existing["latitude"] == lat
                and existing["longitude"] == lon
            )

            if unchanged:
                # Update provenance freshness
                cursor.execute(
                    "UPDATE meters SET fetched_at = ?, payload_hash = ? WHERE meter_id = ?",
                    (fetched_at_str, provenance.payload_hash, meter.meter_id),
                )
                return UpsertResult.UNCHANGED

            # Record changed: perform update
            cursor.execute(
                """
                UPDATE meters SET
                    serial_no = ?,
                    make = ?,
                    phase_type = ?,
                    installation_status = ?,
                    installation_type = ?,
                    dt_code = ?,
                    latitude = ?,
                    longitude = ?,
                    hierarchy_json = COALESCE(?, hierarchy_json),
                    quality_flags = ?,
                    fetched_at = ?,
                    source_endpoint = ?,
                    payload_hash = ?
                WHERE meter_id = ?
                """,
                (
                    meter.serial_no,
                    meter.make,
                    phase,
                    status,
                    meter.installation_type,
                    meter.dt_code,
                    lat,
                    lon,
                    hierarchy_json,
                    quality_flags_json,
                    fetched_at_str,
                    provenance.source_endpoint,
                    provenance.payload_hash,
                    meter.meter_id,
                ),
            )
            return UpsertResult.CHANGED

    def get_meter(self, meter_id: str) -> Meter | None:
        """Fetch a single meter by ID, reconstructing domain submodels."""
        res = self.get_meter_with_provenance(meter_id)
        return res[0] if res else None

    def get_meter_with_provenance(self, meter_id: str) -> tuple[Meter, datetime] | None:
        """Fetch a meter along with its fetched_at timestamp."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT * FROM meters WHERE meter_id = ?",
                (meter_id.strip().upper(),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_meter(row), _iso_to_dt(row["fetched_at"])

    def list_meters(
        self,
        dt_code: str | None = None,
        serial_no: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Meter]:
        """List meters with optional filtering and pagination."""
        conditions: list[str] = []
        params: list[Any] = []

        if dt_code:
            conditions.append("dt_code = ?")
            params.append(dt_code.strip().upper())
        if serial_no:
            conditions.append("serial_no = ?")
            params.append(serial_no.strip())

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM meters {where} ORDER BY meter_id ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(query, params)
            return [self._row_to_meter(r) for r in cursor.fetchall()]

    def list_meters_cursor(
        self,
        cursor: str | None = None,
        limit: int = 20,
        dt_code: str | None = None,
        serial_no: str | None = None,
        make: str | None = None,
        phase_type: str | None = None,
        installation_status: str | None = None,
    ) -> tuple[list[Meter], str | None, bool, datetime | None]:
        """Fetch a page of meters using cursor pagination (meter_id > cursor).

        Returns:
            (items, next_cursor, has_more, latest_fetched_at)
        """
        conditions: list[str] = []
        params: list[Any] = []

        if cursor:
            conditions.append("meter_id > ?")
            params.append(cursor.strip().upper())
        if dt_code:
            conditions.append("dt_code = ?")
            params.append(dt_code.strip().upper())
        if serial_no:
            conditions.append("serial_no = ?")
            params.append(serial_no.strip())
        if make:
            conditions.append("make = ?")
            params.append(make.strip())
        if phase_type:
            conditions.append("phase_type = ?")
            params.append(phase_type.strip().lower())
        if installation_status:
            conditions.append("installation_status = ?")
            params.append(installation_status.strip())

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM meters {where} ORDER BY meter_id ASC LIMIT ?"
        # Fetch limit + 1 to detect next page
        params.append(limit + 1)

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()

            latest_fetched_at = max(_iso_to_dt(r["fetched_at"]) for r in rows) if rows else None
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            meters = [self._row_to_meter(r) for r in page_rows]
            next_cursor = meters[-1].meter_id if has_more and meters else None

            return meters, next_cursor, has_more, latest_fetched_at

    def count_meters(self, dt_code: str | None = None) -> int:
        """Count total meters in store, optionally filtered by DT code."""
        query = "SELECT COUNT(*) FROM meters"
        params: list[Any] = []
        if dt_code:
            query += " WHERE dt_code = ?"
            params.append(dt_code.strip().upper())
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(query, params)
            return int(cursor.fetchone()[0])

    def list_all_meters(self) -> list[Meter]:
        """Fetch all meters in the store."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM meters ORDER BY meter_id ASC")
            return [self._row_to_meter(r) for r in cursor.fetchall()]

    def find_meters_nearby(
        self,
        lat: float,
        lon: float,
        radius_km: float = 5.0,
        limit: int = 20,
    ) -> list[tuple[Meter, float]]:
        """Find meters within radius_km using bounding-box prefilter + haversine distance.

        Excludes Null Island (0,0) and records with missing coordinates.
        Returns list of (Meter, distance_km) sorted by distance ascending.
        """
        # 1. Bounding box prefilter
        lat_delta = radius_km / 111.32
        cos_lat = math.cos(math.radians(lat))
        lon_delta = radius_km / (111.32 * max(0.01, abs(cos_lat)))

        min_lat = lat - lat_delta
        max_lat = lat + lat_delta
        min_lon = lon - lon_delta
        max_lon = lon + lon_delta

        query = """
        SELECT * FROM meters
        WHERE latitude BETWEEN ? AND ?
          AND longitude BETWEEN ? AND ?
          AND NOT (latitude = 0.0 AND longitude = 0.0)
        """
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(query, (min_lat, max_lat, min_lon, max_lon))
            rows = cursor.fetchall()

        # 2. Haversine distance calculation
        r_earth = 6371.0
        results: list[tuple[Meter, float]] = []

        for row in rows:
            m = self._row_to_meter(row)
            if not m.location:
                continue

            m_lat = m.location.latitude
            m_lon = m.location.longitude

            dlat = math.radians(m_lat - lat)
            dlon = math.radians(m_lon - lon)
            a = (
                math.sin(dlat / 2.0) ** 2
                + math.cos(math.radians(lat))
                * math.cos(math.radians(m_lat))
                * math.sin(dlon / 2.0) ** 2
            )
            c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
            dist_km = r_earth * c

            if dist_km <= radius_km:
                results.append((m, round(dist_km, 3)))

        # Sort by distance ascending and slice limit
        results.sort(key=lambda item: item[1])
        return results[:limit]

    def _row_to_meter(self, row: sqlite3.Row) -> Meter:
        """Deserialize database row to Meter domain model."""
        loc = None
        if row["latitude"] is not None and row["longitude"] is not None:
            loc = Location(latitude=row["latitude"], longitude=row["longitude"])

        hierarchy_path = None
        if row["hierarchy_json"]:
            try:
                hierarchy_path = HierarchyPath.model_validate_json(row["hierarchy_json"])
            except Exception as e:
                logger.warning(
                    "Failed to deserialize hierarchy_json for meter %s: %s",
                    row["meter_id"],
                    e,
                )

        phase = PhaseType(row["phase_type"]) if row["phase_type"] else None
        status = (
            InstallationStatus(row["installation_status"]) if row["installation_status"] else None
        )
        flags = json.loads(row["quality_flags"]) if row["quality_flags"] else []

        return Meter(
            meter_id=row["meter_id"],
            serial_no=row["serial_no"],
            make=row["make"],
            phase_type=phase,
            installation_status=status,
            installation_type=row["installation_type"],
            dt_code=row["dt_code"],
            location=loc,
            hierarchy_path=hierarchy_path,
            quality_flags=flags,
        )

    # ── Readings ──────────────────────────────────────────────────────

    def upsert_reading(self, reading: Reading, provenance: Provenance) -> UpsertResult:
        """Idempotently insert or update a consumption reading."""
        ts_str = _dt_to_iso(reading.timestamp)
        fetched_at_str = _dt_to_iso(provenance.fetched_at)
        quality_flags_json = json.dumps(reading.quality_flags)

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT kwh, kvah, voltage_r, payload_hash FROM readings "
                "WHERE meter_id = ? AND timestamp = ?",
                (reading.meter_id, ts_str),
            )
            existing = cursor.fetchone()

            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO readings (
                        meter_id, timestamp, kwh, kvah, voltage_r,
                        quality_flags, fetched_at, source_endpoint, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reading.meter_id,
                        ts_str,
                        reading.kwh,
                        reading.kvah,
                        reading.voltage_r,
                        quality_flags_json,
                        fetched_at_str,
                        provenance.source_endpoint,
                        provenance.payload_hash,
                    ),
                )
                return UpsertResult.NEW

            # Change detection
            unchanged = (
                existing["kwh"] == reading.kwh
                and existing["kvah"] == reading.kvah
                and existing["voltage_r"] == reading.voltage_r
            )

            if unchanged:
                cursor.execute(
                    "UPDATE readings SET fetched_at = ?, payload_hash = ? WHERE meter_id = ? AND timestamp = ?",
                    (fetched_at_str, provenance.payload_hash, reading.meter_id, ts_str),
                )
                return UpsertResult.UNCHANGED

            cursor.execute(
                """
                UPDATE readings SET
                    kwh = ?,
                    kvah = ?,
                    voltage_r = ?,
                    quality_flags = ?,
                    fetched_at = ?,
                    source_endpoint = ?,
                    payload_hash = ?
                WHERE meter_id = ? AND timestamp = ?
                """,
                (
                    reading.kwh,
                    reading.kvah,
                    reading.voltage_r,
                    quality_flags_json,
                    fetched_at_str,
                    provenance.source_endpoint,
                    provenance.payload_hash,
                    reading.meter_id,
                    ts_str,
                ),
            )
            return UpsertResult.CHANGED

    def upsert_readings(
        self, readings: list[Reading], provenance: Provenance
    ) -> tuple[int, int, int]:
        """Batch upsert readings atomically, returning (new, changed, unchanged) counts."""
        new_cnt = 0
        changed_cnt = 0
        unchanged_cnt = 0

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for reading in readings:
                    res = self.upsert_reading(reading, provenance)
                    if res == UpsertResult.NEW:
                        new_cnt += 1
                    elif res == UpsertResult.CHANGED:
                        changed_cnt += 1
                    else:
                        unchanged_cnt += 1
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return new_cnt, changed_cnt, unchanged_cnt

    def get_readings(
        self,
        meter_id: str,
        limit: int = 100,
        since: datetime | None = None,
    ) -> list[Reading]:
        """Fetch consumption readings for a meter ordered by timestamp descending."""
        readings, _ = self.get_readings_range(meter_id=meter_id, since=since, limit=limit)
        return readings

    def get_readings_range(
        self,
        meter_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> tuple[list[Reading], datetime | None]:
        """Fetch readings within date range, returning (readings, latest_fetched_at)."""
        conditions = ["meter_id = ?"]
        params: list[Any] = [meter_id.strip().upper()]

        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(_dt_to_iso(since))
        if until is not None:
            conditions.append("timestamp <= ?")
            params.append(_dt_to_iso(until))

        where = f"WHERE {' AND '.join(conditions)}"
        query = f"SELECT * FROM readings {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            latest_fetched_at = max(_iso_to_dt(r["fetched_at"]) for r in rows) if rows else None
            return [self._row_to_reading(r) for r in rows], latest_fetched_at

    def _row_to_reading(self, row: sqlite3.Row) -> Reading:
        flags = json.loads(row["quality_flags"]) if row["quality_flags"] else []
        return Reading(
            meter_id=row["meter_id"],
            timestamp=_iso_to_dt(row["timestamp"]),
            kwh=row["kwh"],
            kvah=row["kvah"],
            voltage_r=row["voltage_r"],
            quality_flags=flags,
        )

    # ── Hierarchy Nodes ───────────────────────────────────────────────

    def upsert_hierarchy_node(self, node: HierarchyNode, provenance: Provenance) -> UpsertResult:
        """Idempotently insert or update a hierarchy node with change detection."""
        fetched_at_str = _dt_to_iso(provenance.fetched_at)
        quality_flags_json = json.dumps(node.quality_flags)

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT name, level, parent_code, payload_hash FROM hierarchy_nodes WHERE code = ?",
                (node.code,),
            )
            existing = cursor.fetchone()

            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO hierarchy_nodes (
                        code, name, level, parent_code, quality_flags,
                        fetched_at, source_endpoint, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node.code,
                        node.name,
                        node.level.value,
                        node.parent_code,
                        quality_flags_json,
                        fetched_at_str,
                        provenance.source_endpoint,
                        provenance.payload_hash,
                    ),
                )
                return UpsertResult.NEW

            unchanged = (
                existing["name"] == node.name
                and existing["level"] == node.level.value
                and existing["parent_code"] == node.parent_code
            )

            if unchanged:
                return UpsertResult.UNCHANGED

            cursor.execute(
                """
                UPDATE hierarchy_nodes SET
                    name = ?,
                    level = ?,
                    parent_code = ?,
                    quality_flags = ?,
                    fetched_at = ?,
                    source_endpoint = ?,
                    payload_hash = ?
                WHERE code = ?
                """,
                (
                    node.name,
                    node.level.value,
                    node.parent_code,
                    quality_flags_json,
                    fetched_at_str,
                    provenance.source_endpoint,
                    provenance.payload_hash,
                    node.code,
                ),
            )
            return UpsertResult.CHANGED

    def upsert_hierarchy_path(
        self, path: HierarchyPath, provenance: Provenance
    ) -> tuple[int, int, int]:
        """Upsert all non-null nodes in a hierarchy path atomically."""
        new_cnt = 0
        changed_cnt = 0
        unchanged_cnt = 0

        nodes = [
            path.zone,
            path.circle,
            path.division,
            path.subdivision,
            path.substation,
            path.feeder,
            path.dt,
        ]

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for node in nodes:
                    if node is not None:
                        res = self.upsert_hierarchy_node(node, provenance)
                        if res == UpsertResult.NEW:
                            new_cnt += 1
                        elif res == UpsertResult.CHANGED:
                            changed_cnt += 1
                        else:
                            unchanged_cnt += 1
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return new_cnt, changed_cnt, unchanged_cnt

    def get_hierarchy_node(self, code: str) -> HierarchyNode | None:
        """Fetch a single hierarchy node by code."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM hierarchy_nodes WHERE code = ?", (code.strip(),))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_node(row)

    def list_hierarchy_nodes(
        self,
        level: HierarchyLevel | None = None,
        parent_code: str | None = None,
    ) -> list[HierarchyNode]:
        """Query hierarchy nodes filtered by level or parent code."""
        conditions = []
        params: list[Any] = []

        if level is not None:
            conditions.append("level = ?")
            params.append(level.value)
        if parent_code is not None:
            conditions.append("parent_code = ?")
            params.append(parent_code.strip())

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM hierarchy_nodes {where} ORDER BY code ASC"

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(query, params)
            return [self._row_to_node(r) for r in cursor.fetchall()]

    def list_all_nodes(self) -> list[HierarchyNode]:
        """Fetch all hierarchy nodes in the store."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM hierarchy_nodes ORDER BY code ASC")
            return [self._row_to_node(r) for r in cursor.fetchall()]

    def get_node_children(self, parent_code: str) -> list[HierarchyNode]:
        """Fetch immediate direct children of a hierarchy node."""
        norm = parent_code.strip().upper()
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT * FROM hierarchy_nodes WHERE UPPER(TRIM(parent_code)) = ? ORDER BY code ASC",
                (norm,),
            )
            return [self._row_to_node(r) for r in cursor.fetchall()]

    def get_node_subtree_cte(self, root_code: str) -> list[HierarchyNode]:
        """Fetch all descendants of a node using an iterative recursive CTE with cycle protection.

        Includes the root node itself as the first element if found.
        """
        norm = root_code.strip().upper()
        query = """
        WITH RECURSIVE subtree(code, name, level, parent_code, quality_flags, depth) AS (
            SELECT code, name, level, parent_code, quality_flags, 0 AS depth
            FROM hierarchy_nodes
            WHERE UPPER(TRIM(code)) = ?
            UNION ALL
            SELECT h.code, h.name, h.level, h.parent_code, h.quality_flags, s.depth + 1
            FROM hierarchy_nodes h
            JOIN subtree s ON UPPER(TRIM(h.parent_code)) = UPPER(TRIM(s.code))
            WHERE s.depth < 20
        )
        SELECT code, name, level, parent_code, quality_flags
        FROM subtree
        ORDER BY depth ASC, code ASC;
        """
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(query, (norm,))
            return [self._row_to_node(r) for r in cursor.fetchall()]

    def _row_to_node(self, row: sqlite3.Row) -> HierarchyNode:
        flags = json.loads(row["quality_flags"]) if row["quality_flags"] else []
        return HierarchyNode(
            code=row["code"],
            name=row["name"],
            level=HierarchyLevel(row["level"]),
            parent_code=row["parent_code"],
            quality_flags=flags,
        )

    # ── Transformers ──────────────────────────────────────────────────

    def upsert_transformer(self, transformer: Transformer, provenance: Provenance) -> UpsertResult:
        """Idempotently insert or update a transformer with change detection."""
        fetched_at_str = _dt_to_iso(provenance.fetched_at)
        quality_flags_json = json.dumps(transformer.quality_flags)

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT name, feeder, capacity_kva, payload_hash FROM transformers WHERE code = ?",
                (transformer.code,),
            )
            existing = cursor.fetchone()

            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO transformers (
                        code, name, feeder, capacity_kva, quality_flags,
                        fetched_at, source_endpoint, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transformer.code,
                        transformer.name,
                        transformer.feeder,
                        transformer.capacity_kva,
                        quality_flags_json,
                        fetched_at_str,
                        provenance.source_endpoint,
                        provenance.payload_hash,
                    ),
                )
                return UpsertResult.NEW

            unchanged = (
                existing["name"] == transformer.name
                and existing["feeder"] == transformer.feeder
                and existing["capacity_kva"] == transformer.capacity_kva
            )

            if unchanged:
                return UpsertResult.UNCHANGED

            cursor.execute(
                """
                UPDATE transformers SET
                    name = ?,
                    feeder = ?,
                    capacity_kva = ?,
                    quality_flags = ?,
                    fetched_at = ?,
                    source_endpoint = ?,
                    payload_hash = ?
                WHERE code = ?
                """,
                (
                    transformer.name,
                    transformer.feeder,
                    transformer.capacity_kva,
                    quality_flags_json,
                    fetched_at_str,
                    provenance.source_endpoint,
                    provenance.payload_hash,
                    transformer.code,
                ),
            )
            return UpsertResult.CHANGED

    def get_transformer(self, code: str) -> Transformer | None:
        """Fetch transformer by DT code."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM transformers WHERE code = ?", (code.strip().upper(),))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_transformer(row)

    def list_transformers(self) -> list[Transformer]:
        """List all transformers ordered by code."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM transformers ORDER BY code ASC")
            return [self._row_to_transformer(r) for r in cursor.fetchall()]

    def _row_to_transformer(self, row: sqlite3.Row) -> Transformer:
        flags = json.loads(row["quality_flags"]) if row["quality_flags"] else []
        return Transformer(
            code=row["code"],
            name=row["name"],
            feeder=row["feeder"],
            capacity_kva=row["capacity_kva"],
            quality_flags=flags,
        )

    # ── Sync Runs Audit ───────────────────────────────────────────────

    def create_sync_run(self, resource: str, run_id: str | None = None) -> SyncRun:
        """Record the start of a synchronization job."""
        rid = run_id or str(uuid4())
        now = datetime.now(timezone.utc)
        now_str = _dt_to_iso(now)

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO sync_runs (
                    run_id, started_at, status, resource,
                    items_new, items_changed, items_unchanged, items_failed
                ) VALUES (?, ?, ?, ?, 0, 0, 0, 0)
                """,
                (rid, now_str, SyncStatus.RUNNING.value, resource),
            )

        return SyncRun(
            run_id=rid,
            started_at=now,
            status=SyncStatus.RUNNING,
            resource=resource,
        )

    def finish_sync_run(
        self,
        run_id: str,
        status: SyncStatus,
        items_new: int = 0,
        items_changed: int = 0,
        items_unchanged: int = 0,
        items_failed: int = 0,
        error_message: str | None = None,
    ) -> SyncRun | None:
        """Mark a sync run as completed with final counts and status."""
        now = datetime.now(timezone.utc)
        now_str = _dt_to_iso(now)

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                UPDATE sync_runs SET
                    completed_at = ?,
                    status = ?,
                    items_new = ?,
                    items_changed = ?,
                    items_unchanged = ?,
                    items_failed = ?,
                    error_message = ?
                WHERE run_id = ?
                """,
                (
                    now_str,
                    status.value,
                    items_new,
                    items_changed,
                    items_unchanged,
                    items_failed,
                    error_message,
                    run_id,
                ),
            )
            return self.get_sync_run(run_id)

    def get_sync_run(self, run_id: str) -> SyncRun | None:
        """Fetch sync run audit record by ID."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM sync_runs WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_sync_run(row)

    def get_latest_sync_run(self, resource: str | None = None) -> SyncRun | None:
        """Fetch the most recent sync run, optionally filtered by resource."""
        where = "WHERE resource = ?" if resource else ""
        params = [resource] if resource else []
        query = f"SELECT * FROM sync_runs {where} ORDER BY started_at DESC LIMIT 1"

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(query, params)
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_sync_run(row)

    def list_sync_runs(self, limit: int = 20) -> list[SyncRun]:
        """List historical sync runs ordered by start time descending."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?", (limit,))
            return [self._row_to_sync_run(r) for r in cursor.fetchall()]

    def _row_to_sync_run(self, row: sqlite3.Row) -> SyncRun:
        started_at = _iso_to_dt(row["started_at"])
        completed_at = _iso_to_dt(row["completed_at"]) if row["completed_at"] else None
        return SyncRun(
            run_id=row["run_id"],
            started_at=started_at,
            completed_at=completed_at,
            status=SyncStatus(row["status"]),
            resource=row["resource"],
            items_new=row["items_new"],
            items_changed=row["items_changed"],
            items_unchanged=row["items_unchanged"],
            items_failed=row["items_failed"],
            error_message=row["error_message"],
        )
