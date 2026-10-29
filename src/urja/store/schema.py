"""SQLite database schema and index definitions for Urja Meter Ops.

Every index is strictly justified below:
1. idx_meters_dt_code: Filter meters by distribution transformer (hierarchy queries).
2. idx_meters_serial_no: Filter/search meters by serial number (matching portal search).
3. idx_readings_timestamp: Time-range queries across meters (composite PK covers meter_id).
4. idx_hierarchy_nodes_parent: Child lookups during top-down tree traversal.
5. idx_hierarchy_nodes_level: Filtering hierarchy nodes by specific level (e.g. all substations).
6. idx_sync_runs_started_at: Querying latest sync run for freshness checks.
"""

from __future__ import annotations

SCHEMA_SQL = """
-- Pragma settings
PRAGMA foreign_keys = ON;

-- 1. Meters table
CREATE TABLE IF NOT EXISTS meters (
    meter_id TEXT PRIMARY KEY,
    serial_no TEXT,
    make TEXT,
    phase_type TEXT,
    installation_status TEXT,
    installation_type TEXT,
    build TEXT,
    dt_code TEXT,
    latitude REAL,
    longitude REAL,
    hierarchy_json TEXT,
    quality_flags TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL,
    source_endpoint TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);

-- Index: Filter meters by DT code (justification: essential for hierarchy/substation lookups)
CREATE INDEX IF NOT EXISTS idx_meters_dt_code ON meters(dt_code);

-- Index: Search meters by serial number (justification: portal search by serial)
CREATE INDEX IF NOT EXISTS idx_meters_serial_no ON meters(serial_no);


-- 2. Readings table (composite PK prevents duplicates and ensures idempotency)
CREATE TABLE IF NOT EXISTS readings (
    meter_id TEXT NOT NULL REFERENCES meters(meter_id) ON DELETE CASCADE,
    raw_timestamp TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    kwh REAL NOT NULL,
    kvah REAL NOT NULL,
    voltage_r REAL,
    quality_flags TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL,
    source_endpoint TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (meter_id, raw_timestamp)
);

-- Index: Range queries and load aggregation across meters by timestamp
-- (Note: queries by meter_id + raw_timestamp are already covered by the composite primary key)
CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp);


-- 3. Hierarchy Nodes table (keyed by canonical full path to disambiguate identical codes)
CREATE TABLE IF NOT EXISTS hierarchy_nodes (
    node_path TEXT PRIMARY KEY,
    code TEXT,
    name TEXT,
    level TEXT NOT NULL,
    parent_path TEXT,
    quality_flags TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL,
    source_endpoint TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);

-- Index: Fast parent-child lookups for tree traversal (e.g. find all feeders of substation)
CREATE INDEX IF NOT EXISTS idx_hierarchy_nodes_parent ON hierarchy_nodes(parent_path);

-- Index: Filter nodes by tier level (e.g. list all substations or circles)
CREATE INDEX IF NOT EXISTS idx_hierarchy_nodes_level ON hierarchy_nodes(level);

-- Index: Fast lookup by code (even when codes appear under multiple parents)
CREATE INDEX IF NOT EXISTS idx_hierarchy_nodes_code ON hierarchy_nodes(code);


-- 4. Transformers table (/transformers list)
CREATE TABLE IF NOT EXISTS transformers (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    feeder TEXT NOT NULL,
    capacity_kva REAL NOT NULL,
    quality_flags TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL,
    source_endpoint TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);


-- 5. Sync runs table (audit trail of synchronization jobs)
CREATE TABLE IF NOT EXISTS sync_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    resource TEXT NOT NULL,
    items_new INTEGER NOT NULL DEFAULT 0,
    items_changed INTEGER NOT NULL DEFAULT 0,
    items_unchanged INTEGER NOT NULL DEFAULT 0,
    items_failed INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

-- Index: Fast retrieval of most recent sync run for freshness and monitoring
CREATE INDEX IF NOT EXISTS idx_sync_runs_started_at ON sync_runs(started_at DESC);
"""
