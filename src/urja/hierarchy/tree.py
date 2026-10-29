"""Network hierarchy tree construction and anomaly detection.

Reconstructs the 7-level utility tree from messy portal data with deterministic
repair rules:
- ID normalization: trims whitespace and canonicalizes to uppercase
- Multiple parents: picks lexically first parent; flags `multiple_parents:[...]`
- Cycles: breaks back-edge; sets parent to None; flags `cycle_detected:...`
- Dangling parents: parent code missing from dataset; flags `dangling_parent:...`
- Orphans: non-Zone nodes without a parent; flags `orphan_node`
- Never deletes nodes: every node is retained in the graph and quality report

Data scale note:
In-memory tree synthesis and recursive CTEs run in O(V + E) time, which is
instantaneous for typical utility districts (tens of thousands of nodes).
However, for nation-scale grids with >10M meters and deep multi-tenant hierarchies,
this approach struggles due to SQLite's lack of native spatial R-tree indexing
and memory limits on recursive CTE breadth. At that scale, a dedicated graph/spatial
datastore (e.g. PostgreSQL with PostGIS and recursive CTE materialized paths, or Neo4j)
is required.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from urja.domain.models import (
    HierarchyLevel,
    HierarchyNode,
    Meter,
    QualityReport,
    Reading,
    TreeNode,
)

logger = logging.getLogger(__name__)


def normalize_code(raw: str | None) -> str | None:
    """Normalize node code by stripping whitespace and converting to uppercase."""
    if raw is None:
        return None
    cleaned = raw.strip().upper()
    return cleaned if cleaned else None


class HierarchyGraph:
    """Builds and analyzes a network tree from dirty node records."""

    def __init__(self, nodes: list[HierarchyNode], meters: list[Meter] | None = None) -> None:
        self.raw_nodes = nodes
        self.meters = meters or []
        self._meters_by_dt: dict[str, list[str]] = defaultdict(list)
        for m in self.meters:
            if m.dt_code:
                self._meters_by_dt[normalize_code(m.dt_code) or ""].append(m.meter_id)

    def analyze_and_build(self) -> tuple[list[TreeNode], dict[str, Any]]:
        """Process nodes, detect anomalies, apply repair rules, and construct tree."""
        # 1. Deduplicate by code and track multiple parents
        parents_by_code: dict[str, set[str]] = defaultdict(set)
        node_by_code: dict[str, HierarchyNode] = {}
        id_mismatches: dict[str, list[str]] = defaultdict(list)

        for n in self.raw_nodes:
            norm_c = normalize_code(n.code)
            if not norm_c:
                continue

            # Check whitespace/case mismatch
            if n.code != norm_c:
                id_mismatches[norm_c].append(n.code)

            norm_p = normalize_code(n.parent_code)
            if n.parent_code and n.parent_code != norm_p:
                id_mismatches[norm_c].append(f"parent:{n.parent_code}")

            if norm_p:
                parents_by_code[norm_c].add(norm_p)

            # Keep newest or first seen
            if norm_c not in node_by_code:
                node_by_code[norm_c] = n

        all_known_codes = set(node_by_code.keys())

        # 2. Anomaly detection & deterministic attachment
        effective_parent: dict[str, str | None] = {}
        flags_by_code: dict[str, list[str]] = defaultdict(list)

        defects: dict[str, Any] = {
            "multiple_parents": {},
            "dangling_parents": {},
            "orphans": [],
            "cycles": [],
            "id_mismatches": dict(id_mismatches),
        }

        for code, node in node_by_code.items():
            flags = list(node.quality_flags)

            if code in id_mismatches:
                flags.append(f"id_mismatch:{','.join(id_mismatches[code])}")

            parents = sorted(parents_by_code.get(code, []))

            # Multiple parents: deterministic rule -> pick lexically first parent
            if len(parents) > 1:
                chosen_parent = parents[0]
                flags.append(f"multiple_parents:{','.join(parents)}")
                defects["multiple_parents"][code] = parents
            elif len(parents) == 1:
                chosen_parent = parents[0]
            else:
                chosen_parent = None

            # Dangling parent: parent code is not in dataset
            if chosen_parent is not None and chosen_parent not in all_known_codes:
                flags.append(f"dangling_parent:{chosen_parent}")
                defects["dangling_parents"][code] = chosen_parent
                chosen_parent = None  # Detach to top-level to prevent broken edges

            # Orphan: non-Zone node with no parent
            if chosen_parent is None and node.level != HierarchyLevel.ZONE:
                flags.append("orphan_node")
                defects["orphans"].append(code)

            effective_parent[code] = chosen_parent
            flags_by_code[code] = flags

        # 3. Cycle detection via DFS path tracking
        # Deterministic rule: break cycle by setting effective_parent[node] = None
        parent_map = dict(effective_parent)

        for code in list(node_by_code.keys()):
            curr = code
            path = []
            seen_in_path = set()

            while curr is not None:
                if curr in seen_in_path:
                    # Cycle detected!
                    cycle_idx = path.index(curr)
                    cycle_path = [*path[cycle_idx:], curr]
                    cycle_str = "->".join(cycle_path)
                    logger.warning("Hierarchy cycle detected: %s", cycle_str)
                    defects["cycles"].append(cycle_str)

                    # Break the cycle at the current node
                    effective_parent[curr] = None
                    parent_map[curr] = None
                    flags_by_code[curr].append(f"cycle_detected:{cycle_str}")
                    break

                seen_in_path.add(curr)
                path.append(curr)
                curr = parent_map.get(curr)

        # 4. Construct tree graph
        children_by_parent: dict[str | None, list[str]] = defaultdict(list)
        for code, parent in effective_parent.items():
            children_by_parent[parent].append(code)

        def build_subtree(code: str) -> TreeNode:
            base_node = node_by_code[code]
            child_codes = sorted(children_by_parent.get(code, []))
            children = [build_subtree(c) for c in child_codes]
            meter_ids = sorted(self._meters_by_dt.get(code, []))

            return TreeNode(
                code=code,
                name=base_node.name,
                level=base_node.level,
                parent_code=effective_parent.get(code),
                quality_flags=flags_by_code[code],
                children=children,
                meter_ids=meter_ids,
            )

        root_codes = sorted(children_by_parent.get(None, []))
        tree = [build_subtree(rc) for rc in root_codes]

        return tree, defects


def audit_network_quality(
    nodes: list[HierarchyNode],
    meters: list[Meter],
    readings: list[Reading] | None = None,
) -> QualityReport:
    """Generate a comprehensive data quality defect report across all entities."""
    readings = readings or []
    graph = HierarchyGraph(nodes, meters)
    _, hierarchy_defects = graph.analyze_and_build()

    # Meter defects
    meter_defects: dict[str, Any] = {
        "null_island": [],
        "unknown_phase": [],
        "unknown_status": [],
        "missing_nameplate": [],
        "incomplete_hierarchy": [],
    }

    clean_meters = 0
    for m in meters:
        has_defect = False
        for flag in m.quality_flags:
            has_defect = True
            if "null_island" in flag:
                meter_defects["null_island"].append(m.meter_id)
            elif "unknown_phase" in flag:
                meter_defects["unknown_phase"].append(m.meter_id)
            elif "unknown_status" in flag:
                meter_defects["unknown_status"].append(m.meter_id)
            elif "missing_nameplate" in flag:
                meter_defects["missing_nameplate"].append(m.meter_id)
            elif "incomplete_hierarchy" in flag:
                meter_defects["incomplete_hierarchy"].append(m.meter_id)

        if not has_defect:
            clean_meters += 1

    # Consumption defects
    consumption_defects: dict[str, Any] = {
        "non_monotonic_kwh": [],
        "timestamp_conversion_error": [],
    }
    for r in readings:
        for flag in r.quality_flags:
            if "non_monotonic_kwh" in flag:
                consumption_defects["non_monotonic_kwh"].append(
                    f"{r.meter_id}@{r.timestamp.isoformat()}"
                )
            elif "invalid_timestamp" in flag or "timestamp_format_error" in flag:
                consumption_defects["timestamp_conversion_error"].append(r.meter_id)

    total_audited = len(meters) + len(nodes)
    defective_count = (
        len(hierarchy_defects.get("orphans", []))
        + len(hierarchy_defects.get("cycles", []))
        + len(hierarchy_defects.get("multiple_parents", {}))
        + len(hierarchy_defects.get("dangling_parents", {}))
        + len(meter_defects["null_island"])
        + len(meter_defects["unknown_phase"])
        + len(meter_defects["unknown_status"])
    )

    clean_count = max(0, total_audited - defective_count)
    score = round((clean_count / total_audited) * 100.0, 2) if total_audited > 0 else 100.0

    return QualityReport(
        total_meters_audited=len(meters),
        total_nodes_audited=len(nodes),
        hierarchy_defects=hierarchy_defects,
        meter_defects=meter_defects,
        consumption_defects=consumption_defects,
        quality_score=score,
    )
