"""Tests for network hierarchy tree construction, defect auditing, and spatial queries.

Covers:
- Graph anomaly detection: orphans, cycles, multiple parents, ID mismatches, dangling refs
- Deterministic repair rules and non-deletion invariant
- SQLite recursive CTE subtree queries with depth guard
- Two-phase spatial meter queries (bounding-box prefilter + haversine distance)
- FastAPI /v1 endpoints: /hierarchy, /nodes/{id}/children, /nodes/{id}/subtree, /quality, /meters/nearby
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from urja.api.app import app
from urja.api.deps import get_freshness, get_store
from urja.domain.models import (
    HierarchyLevel,
    HierarchyNode,
    HierarchyPath,
    Location,
    Meter,
    Provenance,
    Reading,
)
from urja.freshness.manager import FreshnessManager
from urja.hierarchy.tree import HierarchyGraph, audit_network_quality, normalize_code
from urja.store.repository import Store

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def memory_store() -> Store:
    store = Store(":memory:")
    yield store
    store.close()


@pytest.fixture
def mock_provenance() -> Provenance:
    return Provenance(
        fetched_at=datetime.now(timezone.utc),
        source_endpoint="/mock",
        payload_hash="test-hash",
    )


# ── HierarchyGraph & Anomaly Detection Unit Tests ─────────────────────


def test_normalize_code():
    assert normalize_code("  delhi_north  ") == "DELHI_NORTH"
    assert normalize_code("dt-001") == "DT-001"
    assert normalize_code("") is None
    assert normalize_code(None) is None


def test_deterministic_multiple_parents_resolution():
    """When a node has multiple parents, pick lexically first parent and flag it."""
    nodes = [
        HierarchyNode(code="Z-NORTH", name="North Zone", level=HierarchyLevel.ZONE),
        HierarchyNode(
            code="C-A", name="Circle A", level=HierarchyLevel.CIRCLE, parent_code="Z-NORTH"
        ),
        HierarchyNode(
            code="C-B", name="Circle B", level=HierarchyLevel.CIRCLE, parent_code="Z-NORTH"
        ),
        # Division D-1 erroneously reported under both C-B and C-A
        HierarchyNode(
            code="D-1", name="Division 1", level=HierarchyLevel.DIVISION, parent_code="C-B"
        ),
        HierarchyNode(
            code="D-1", name="Division 1", level=HierarchyLevel.DIVISION, parent_code="C-A"
        ),
    ]

    graph = HierarchyGraph(nodes)
    tree, defects = graph.analyze_and_build()

    assert "D-1" in defects["multiple_parents"]
    assert defects["multiple_parents"]["D-1"] == ["C-A", "C-B"]

    # In tree, D-1 must be attached to C-A (lexically first)
    circle_a = next(c for c in tree[0].children if c.code == "C-A")
    child_codes = [c.code for c in circle_a.children]
    assert "D-1" in child_codes

    # Quality flag recorded on D-1
    d1_node = next(c for c in circle_a.children if c.code == "D-1")
    assert any("multiple_parents:C-A,C-B" in f for f in d1_node.quality_flags)


def test_cycle_detection_and_breaking():
    """Detect circular references (A -> B -> C -> A), break cycle, and preserve all nodes."""
    nodes = [
        HierarchyNode(
            code="NODE-A", name="Node A", level=HierarchyLevel.SUBDIVISION, parent_code="NODE-C"
        ),
        HierarchyNode(
            code="NODE-B", name="Node B", level=HierarchyLevel.SUBDIVISION, parent_code="NODE-A"
        ),
        HierarchyNode(
            code="NODE-C", name="Node C", level=HierarchyLevel.SUBDIVISION, parent_code="NODE-B"
        ),
    ]

    graph = HierarchyGraph(nodes)
    tree, defects = graph.analyze_and_build()

    assert len(defects["cycles"]) >= 1
    # Check that cycle was broken and nodes still exist (no nodes deleted)
    all_codes = set()

    def collect(node):
        all_codes.add(node.code)
        for c in node.children:
            collect(c)

    for root in tree:
        collect(root)

    assert all_codes == {"NODE-A", "NODE-B", "NODE-C"}


def test_dangling_parent_detachment():
    """A node pointing to a non-existent parent must be detached to root and flagged."""
    nodes = [
        HierarchyNode(code="Z-NORTH", name="North Zone", level=HierarchyLevel.ZONE),
        HierarchyNode(
            code="DIV-GHOST",
            name="Ghost Div",
            level=HierarchyLevel.DIVISION,
            parent_code="NONEXISTENT-CIRCLE",
        ),
    ]

    graph = HierarchyGraph(nodes)
    tree, defects = graph.analyze_and_build()

    assert "DIV-GHOST" in defects["dangling_parents"]
    assert defects["dangling_parents"]["DIV-GHOST"] == "NONEXISTENT-CIRCLE"

    # Both Z-NORTH and DIV-GHOST should be root nodes now
    root_codes = [r.code for r in tree]
    assert "Z-NORTH" in root_codes
    assert "DIV-GHOST" in root_codes

    ghost_node = next(r for r in tree if r.code == "DIV-GHOST")
    assert any("dangling_parent:NONEXISTENT-CIRCLE" in f for f in ghost_node.quality_flags)


def test_orphan_node_detection():
    """Non-Zone nodes without a parent are flagged as orphans."""
    nodes = [
        HierarchyNode(
            code="Z-NORTH", name="North Zone", level=HierarchyLevel.ZONE
        ),  # Zone without parent is normal
        HierarchyNode(code="DT-LOST", name="Lost DT", level=HierarchyLevel.DT, parent_code=None),
    ]

    graph = HierarchyGraph(nodes)
    tree, defects = graph.analyze_and_build()

    assert "DT-LOST" in defects["orphans"]
    assert "Z-NORTH" not in defects["orphans"]

    dt_node = next(r for r in tree if r.code == "DT-LOST")
    assert any("orphan_node" in f for f in dt_node.quality_flags)


def test_id_mismatch_normalization():
    """Trims whitespace and canonicalizes casing while flagging the inconsistency."""
    nodes = [
        HierarchyNode(
            code="  dt-001  ", name="DT 001", level=HierarchyLevel.DT, parent_code="  fdr-99  "
        ),
        HierarchyNode(code="FDR-99", name="Feeder 99", level=HierarchyLevel.FEEDER),
    ]

    graph = HierarchyGraph(nodes)
    tree, defects = graph.analyze_and_build()

    assert "DT-001" in defects["id_mismatches"]
    fdr_node = next(r for r in tree if r.code == "FDR-99")
    assert any(c.code == "DT-001" for c in fdr_node.children)


def test_non_deletion_guarantee():
    """Every input node must appear in the reconstructed tree, regardless of defects."""
    nodes = [
        HierarchyNode(code="Z-1", name="Zone 1", level=HierarchyLevel.ZONE),
        HierarchyNode(code="C-1", name="Circle 1", level=HierarchyLevel.CIRCLE, parent_code="Z-1"),
        HierarchyNode(
            code="D-CYCLE-1",
            name="Cycle 1",
            level=HierarchyLevel.DIVISION,
            parent_code="D-CYCLE-2",
        ),
        HierarchyNode(
            code="D-CYCLE-2",
            name="Cycle 2",
            level=HierarchyLevel.DIVISION,
            parent_code="D-CYCLE-1",
        ),
        HierarchyNode(
            code="DT-DANGLING", name="Dangling DT", level=HierarchyLevel.DT, parent_code="MISSING"
        ),
        HierarchyNode(
            code="DT-ORPHAN", name="Orphan DT", level=HierarchyLevel.DT, parent_code=None
        ),
    ]

    graph = HierarchyGraph(nodes)
    tree, _ = graph.analyze_and_build()

    collected_codes = set()

    def walk(node):
        collected_codes.add(node.code)
        for child in node.children:
            walk(child)

    for r in tree:
        walk(r)

    assert collected_codes == {"Z-1", "C-1", "D-CYCLE-1", "D-CYCLE-2", "DT-DANGLING", "DT-ORPHAN"}


def test_meter_association_to_dt():
    """Meters are associated with their corresponding DT nodes in the tree."""
    nodes = [
        HierarchyNode(code="DT-100", name="DT 100", level=HierarchyLevel.DT),
    ]
    meters = [
        Meter(meter_id="M-01", serial_no="S-01", make="HPL", dt_code="dt-100"),
        Meter(meter_id="M-02", serial_no="S-02", make="HPL", dt_code="DT-100"),
        Meter(meter_id="M-03", serial_no="S-03", make="HPL", dt_code="OTHER-DT"),
    ]

    graph = HierarchyGraph(nodes, meters)
    tree, _ = graph.analyze_and_build()

    assert tree[0].code == "DT-100"
    assert tree[0].meter_ids == ["M-01", "M-02"]


def test_audit_network_quality():
    """Verifies defect reporting across hierarchy, meters, and readings."""
    nodes = [
        HierarchyNode(code="DT-ORPHAN", name="Orphan DT", level=HierarchyLevel.DT),
    ]
    meters = [
        Meter(
            meter_id="M-BAD-LOC",
            serial_no="S-01",
            make="HPL",
            location=Location(latitude=0.0, longitude=0.0),
            quality_flags=["null_island_coordinates"],
        ),
        Meter(
            meter_id="M-CLEAN",
            serial_no="S-02",
            make="HPL",
            location=Location(latitude=28.5, longitude=77.2),
        ),
    ]
    readings = [
        Reading(
            meter_id="M-BAD-LOC",
            timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            kwh=10.0,
            kvah=12.0,
            quality_flags=["non_monotonic_kwh"],
        )
    ]

    report = audit_network_quality(nodes, meters, readings)
    assert report.total_meters_audited == 2
    assert report.total_nodes_audited == 1
    assert "DT-ORPHAN" in report.hierarchy_defects["orphans"]
    assert "M-BAD-LOC" in report.meter_defects["null_island"]
    assert len(report.consumption_defects["non_monotonic_kwh"]) == 1
    assert 0.0 <= report.quality_score <= 100.0


# ── SQLite Recursive CTE Subtree Tests ────────────────────────────────


def test_sqlite_recursive_cte_subtree(memory_store: Store, mock_provenance: Provenance):
    """Verifies that get_node_subtree_cte returns descendants via SQLite recursive CTE."""
    nodes = [
        HierarchyNode(code="ZONE-NORTH", name="North Zone", level=HierarchyLevel.ZONE),
        HierarchyNode(
            code="CIRCLE-DELHI",
            name="Delhi Circle",
            level=HierarchyLevel.CIRCLE,
            parent_code="ZONE-NORTH",
        ),
        HierarchyNode(
            code="DIV-CENTRAL",
            name="Central Div",
            level=HierarchyLevel.DIVISION,
            parent_code="CIRCLE-DELHI",
        ),
        HierarchyNode(
            code="SUBDIV-CP",
            name="CP SubDiv",
            level=HierarchyLevel.SUBDIVISION,
            parent_code="DIV-CENTRAL",
        ),
        HierarchyNode(
            code="SUBST-CP",
            name="CP Substation",
            level=HierarchyLevel.SUBSTATION,
            parent_code="SUBDIV-CP",
        ),
        HierarchyNode(
            code="FDR-101", name="Feeder 101", level=HierarchyLevel.FEEDER, parent_code="SUBST-CP"
        ),
        HierarchyNode(
            code="DT-500", name="DT 500", level=HierarchyLevel.DT, parent_code="FDR-101"
        ),
    ]

    for n in nodes:
        memory_store.upsert_hierarchy_node(n, mock_provenance)

    # Subtree from ZONE-NORTH should return all 7 nodes
    subtree = memory_store.get_node_subtree_cte("ZONE-NORTH")
    assert len(subtree) == 7
    codes = [n.code for n in subtree]
    assert codes[0] == "ZONE-NORTH"
    assert "DT-500" in codes

    # Subtree from SUBST-CP should return 3 nodes (SUBST-CP, FDR-101, DT-500)
    sub_cp = memory_store.get_node_subtree_cte("SUBST-CP")
    assert len(sub_cp) == 3
    assert [n.code for n in sub_cp] == ["SUBST-CP", "FDR-101", "DT-500"]

    # Non-existent node returns empty list
    assert memory_store.get_node_subtree_cte("NON-EXISTENT") == []


def test_sqlite_recursive_cte_cycle_guard(memory_store: Store, mock_provenance: Provenance):
    """Verifies that the CTE depth guard prevents infinite loops on circular parent references."""
    # Deliberate cycle in database: A -> B -> A
    memory_store.upsert_hierarchy_node(
        HierarchyNode(
            code="CYC-A", name="Node A", level=HierarchyLevel.SUBDIVISION, parent_code="CYC-B"
        ),
        mock_provenance,
    )
    memory_store.upsert_hierarchy_node(
        HierarchyNode(
            code="CYC-B", name="Node B", level=HierarchyLevel.SUBDIVISION, parent_code="CYC-A"
        ),
        mock_provenance,
    )

    # Querying subtree must terminate safely within depth limit (< 20)
    subtree = memory_store.get_node_subtree_cte("CYC-A")
    assert len(subtree) > 0
    assert len(subtree) <= 25  # depth guard stops recursion safely


def test_sqlite_get_node_children(memory_store: Store, mock_provenance: Provenance):
    """Verifies get_node_children fetches immediate direct children case-insensitively."""
    memory_store.upsert_hierarchy_node(
        HierarchyNode(code="Z-1", name="Zone 1", level=HierarchyLevel.ZONE),
        mock_provenance,
    )
    memory_store.upsert_hierarchy_node(
        HierarchyNode(code="C-1", name="Circle 1", level=HierarchyLevel.CIRCLE, parent_code="Z-1"),
        mock_provenance,
    )
    memory_store.upsert_hierarchy_node(
        HierarchyNode(code="C-2", name="Circle 2", level=HierarchyLevel.CIRCLE, parent_code="z-1"),
        mock_provenance,
    )

    children = memory_store.get_node_children("z-1")
    assert len(children) == 2
    assert {c.code for c in children} == {"C-1", "C-2"}


# ── Nearby Spatial Query Tests ────────────────────────────────────────


def test_find_meters_nearby(memory_store: Store, mock_provenance: Provenance):
    """Verifies bounding box prefilter + haversine distance filtering and sorting."""
    # Base location: Connaught Place, New Delhi (28.6315, 77.2167)
    # Nearby meter (~1.1 km away): 28.6415, 77.2167
    # Far meter (~30 km away): Gurgaon (28.4595, 77.0266)
    # Null island meter: (0.0, 0.0) -> must be excluded

    m_near = Meter(
        meter_id="M-NEAR",
        serial_no="SN-01",
        make="HPL",
        location=Location(latitude=28.6415, longitude=77.2167),
    )
    m_far = Meter(
        meter_id="M-FAR",
        serial_no="SN-02",
        make="HPL",
        location=Location(latitude=28.4595, longitude=77.0266),
    )
    m_null = Meter(
        meter_id="M-NULL",
        serial_no="SN-03",
        make="HPL",
        location=Location(latitude=0.0, longitude=0.0),
    )
    m_no_loc = Meter(
        meter_id="M-NOLOC",
        serial_no="SN-04",
        make="HPL",
        location=None,
    )

    for m in [m_near, m_far, m_null, m_no_loc]:
        memory_store.upsert_meter(m, mock_provenance)

    # Search within 5 km of Connaught Place
    results = memory_store.find_meters_nearby(lat=28.6315, lon=77.2167, radius_km=5.0)

    assert len(results) == 1
    found_meter, dist_km = results[0]
    assert found_meter.meter_id == "M-NEAR"
    assert 1.0 < dist_km < 1.3  # approx 1.11 km

    # Search within 50 km to include both near and far
    results_50 = memory_store.find_meters_nearby(lat=28.6315, lon=77.2167, radius_km=50.0)
    assert len(results_50) == 2
    # Ensure ordered by distance ascending
    assert results_50[0][0].meter_id == "M-NEAR"
    assert results_50[1][0].meter_id == "M-FAR"
    assert results_50[0][1] < results_50[1][1]


# ── FastAPI Endpoints Integration Tests ───────────────────────────────


@pytest.fixture
def test_app(memory_store: Store):
    freshness = FreshnessManager()
    app.dependency_overrides[get_store] = lambda: memory_store
    app.dependency_overrides[get_freshness] = lambda: freshness
    yield app
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_hierarchy_endpoint(test_app, memory_store: Store, mock_provenance: Provenance):
    """GET /v1/hierarchy returns complete reconstructed network tree."""
    memory_store.upsert_hierarchy_node(
        HierarchyNode(code="ZONE-1", name="Zone 1", level=HierarchyLevel.ZONE),
        mock_provenance,
    )
    memory_store.upsert_hierarchy_node(
        HierarchyNode(
            code="CIR-1", name="Circle 1", level=HierarchyLevel.CIRCLE, parent_code="ZONE-1"
        ),
        mock_provenance,
    )

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/v1/hierarchy")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert len(body["data"]) == 1
        assert body["data"][0]["code"] == "ZONE-1"
        assert len(body["data"][0]["children"]) == 1
        assert body["data"][0]["children"][0]["code"] == "CIR-1"
        assert body["meta"]["source"] == "store"


@pytest.mark.asyncio
async def test_api_nodes_children_endpoint(
    test_app, memory_store: Store, mock_provenance: Provenance
):
    """GET /v1/nodes/{id}/children returns direct children or 404 RFC 7807 problem."""
    memory_store.upsert_hierarchy_node(
        HierarchyNode(code="FDR-1", name="Feeder 1", level=HierarchyLevel.FEEDER),
        mock_provenance,
    )
    memory_store.upsert_hierarchy_node(
        HierarchyNode(code="DT-1A", name="DT 1A", level=HierarchyLevel.DT, parent_code="FDR-1"),
        mock_provenance,
    )

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Success case
        resp = await client.get("/v1/nodes/FDR-1/children")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["code"] == "DT-1A"

        # Not found case -> RFC 7807 problem
        resp_404 = await client.get("/v1/nodes/UNKNOWN/children")
        assert resp_404.status_code == 404
        assert resp_404.headers["content-type"] == "application/problem+json"
        assert resp_404.json()["title"] == "Node Not Found"


@pytest.mark.asyncio
async def test_api_nodes_subtree_endpoint(
    test_app, memory_store: Store, mock_provenance: Provenance
):
    """GET /v1/nodes/{id}/subtree executes recursive CTE or returns 404."""
    path = HierarchyPath(
        zone=HierarchyNode(code="Z-1", name="Zone 1", level=HierarchyLevel.ZONE),
        circle=HierarchyNode(
            code="C-1", name="Circle 1", level=HierarchyLevel.CIRCLE, parent_code="Z-1"
        ),
        division=HierarchyNode(
            code="D-1", name="Div 1", level=HierarchyLevel.DIVISION, parent_code="C-1"
        ),
        subdivision=HierarchyNode(
            code="SD-1", name="SubDiv 1", level=HierarchyLevel.SUBDIVISION, parent_code="D-1"
        ),
        substation=HierarchyNode(
            code="SS-1", name="Substation 1", level=HierarchyLevel.SUBSTATION, parent_code="SD-1"
        ),
        feeder=HierarchyNode(
            code="F-1", name="Feeder 1", level=HierarchyLevel.FEEDER, parent_code="SS-1"
        ),
        dt=HierarchyNode(code="DT-1", name="DT 1", level=HierarchyLevel.DT, parent_code="F-1"),
    )
    memory_store.upsert_hierarchy_path(path, mock_provenance)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/v1/nodes/Z-1/subtree")
        assert resp.status_code == 200
        subtree = resp.json()["data"]
        assert len(subtree) == 7
        assert [n["code"] for n in subtree] == ["Z-1", "C-1", "D-1", "SD-1", "SS-1", "F-1", "DT-1"]

        # Subtree from Feeder
        resp_f = await client.get("/v1/nodes/F-1/subtree")
        assert resp_f.status_code == 200
        assert len(resp_f.json()["data"]) == 2

        # Missing node 404
        resp_404 = await client.get("/v1/nodes/GHOST/subtree")
        assert resp_404.status_code == 404
        assert resp_404.headers["content-type"] == "application/problem+json"


@pytest.mark.asyncio
async def test_api_quality_endpoint(test_app, memory_store: Store, mock_provenance: Provenance):
    """GET /v1/quality returns defect report across network entities."""
    memory_store.upsert_hierarchy_node(
        HierarchyNode(code="DT-ORPHAN", name="Lost DT", level=HierarchyLevel.DT),
        mock_provenance,
    )
    memory_store.upsert_meter(
        Meter(meter_id="M-01", serial_no="S-01", make="HPL", quality_flags=["unknown_phase:4"]),
        mock_provenance,
    )

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/v1/quality")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total_meters_audited"] == 1
        assert data["total_nodes_audited"] == 1
        assert "DT-ORPHAN" in data["hierarchy_defects"]["orphans"]
        assert "M-01" in data["meter_defects"]["unknown_phase"]
        assert "quality_score" in data


@pytest.mark.asyncio
async def test_api_meters_nearby_endpoint(
    test_app, memory_store: Store, mock_provenance: Provenance
):
    """GET /v1/meters/nearby calculates distance and validates input params."""
    meter = Meter(
        meter_id="M-CP",
        serial_no="SN-CP",
        make="HPL",
        location=Location(latitude=28.6315, longitude=77.2167),
    )
    memory_store.upsert_meter(meter, mock_provenance)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Successful search within 1 km
        resp = await client.get("/v1/meters/nearby?lat=28.6310&lon=77.2160&radius_km=1.0")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["meter"]["meter_id"] == "M-CP"
        assert data[0]["distance_km"] < 0.2

        # Invalid latitude (> 90) -> 422 validation problem
        resp_invalid = await client.get("/v1/meters/nearby?lat=95.0&lon=77.0")
        assert resp_invalid.status_code == 422
        assert resp_invalid.headers["content-type"] == "application/problem+json"
