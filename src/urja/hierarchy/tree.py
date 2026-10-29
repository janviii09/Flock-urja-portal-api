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
        # 1. Deduplicate by unique key (node_path if present, else normalized code)
        parents_by_key: dict[str, set[str]] = defaultdict(set)
        parents_by_code: dict[str, set[str]] = defaultdict(set)
        node_by_key: dict[str, HierarchyNode] = {}
        id_mismatches: dict[str, list[str]] = defaultdict(list)

        for n in self.raw_nodes:
            norm_c = normalize_code(n.code)
            norm_p = normalize_code(n.parent_code)

            key = normalize_code(n.node_path) or norm_c or ""
            if not key:
                continue

            if norm_c and n.code != norm_c:
                id_mismatches[norm_c].append(n.code)

            if n.parent_code and norm_p and n.parent_code != norm_p:
                id_mismatches[norm_c].append(f"parent:{n.parent_code}")

            p_key = normalize_code(n.parent_path) or norm_p or ""
            if norm_c and p_key:
                norm_parent = norm_p or p_key.split("/")[-1]
                parents_by_code[norm_c].add(norm_parent)

            if p_key:
                parents_by_key[key].add(p_key)

            if key not in node_by_key:
                node_by_key[key] = n

        all_known_keys = set(node_by_key.keys())

        # 2. Anomaly detection & deterministic attachment
        effective_parent: dict[str, str | None] = {}
        flags_by_key: dict[str, list[str]] = defaultdict(list)

        defects: dict[str, Any] = {
            "multiple_parents": {},
            "dangling_parents": {},
            "orphans": [],
            "cycles": [],
            "id_mismatches": dict(id_mismatches),
        }

        # Track multiple parents at the code level for audit reporting
        for code, code_parents in sorted(parents_by_code.items()):
            if len(code_parents) > 1:
                defects["multiple_parents"][code] = sorted(code_parents)

        for key, node in node_by_key.items():
            flags = list(node.quality_flags)

            norm_c = normalize_code(node.code)
            if norm_c and norm_c in id_mismatches:
                flags.append(f"id_mismatch:{','.join(id_mismatches[norm_c])}")
            elif key in id_mismatches:
                flags.append(f"id_mismatch:{','.join(id_mismatches[key])}")

            parents = sorted(parents_by_key.get(key, []))

            # Multiple parents for the same key: pick lexically first
            if len(parents) > 1:
                chosen_parent = parents[0]
                flags.append(f"multiple_parents:{','.join(parents)}")
                norm_c = normalize_code(node.code) or key
                if norm_c not in defects["multiple_parents"]:
                    defects["multiple_parents"][norm_c] = parents
            elif len(parents) == 1:
                chosen_parent = parents[0]
            else:
                chosen_parent = None

            # Also check if this node's code was flagged with multiple parents
            norm_c = normalize_code(node.code)
            if (
                norm_c
                and norm_c in defects["multiple_parents"]
                and not any("multiple_parents:" in f for f in flags)
            ):
                flags.append(f"multiple_parents:{','.join(defects['multiple_parents'][norm_c])}")

            # Dangling parent: parent key is not in dataset
            if chosen_parent is not None and chosen_parent not in all_known_keys:
                flags.append(f"dangling_parent:{chosen_parent}")
                defects["dangling_parents"][key] = chosen_parent
                chosen_parent = None  # Detach to top-level to prevent broken edges

            # Orphan: non-Zone node with no parent
            if chosen_parent is None and node.level != HierarchyLevel.ZONE:
                flags.append("orphan_node")
                defects["orphans"].append(key)

            effective_parent[key] = chosen_parent
            flags_by_key[key] = flags

        # 3. Cycle detection via DFS path tracking
        parent_map = dict(effective_parent)

        for key in list(node_by_key.keys()):
            curr = key
            path = []
            seen_in_path = set()

            while curr is not None:
                if curr in seen_in_path:
                    cycle_idx = path.index(curr)
                    cycle_path = [*path[cycle_idx:], curr]
                    cycle_str = "->".join(cycle_path)
                    logger.warning("Hierarchy cycle detected: %s", cycle_str)
                    defects["cycles"].append(cycle_str)

                    effective_parent[curr] = None
                    parent_map[curr] = None
                    flags_by_key[curr].append(f"cycle_detected:{cycle_str}")
                    break

                seen_in_path.add(curr)
                path.append(curr)
                curr = parent_map.get(curr)

        # 4. Construct tree graph
        children_by_parent: dict[str | None, list[str]] = defaultdict(list)
        for key, parent in effective_parent.items():
            children_by_parent[parent].append(key)

        def build_subtree(key: str) -> TreeNode:
            base_node = node_by_key[key]
            child_keys = sorted(children_by_parent.get(key, []))
            children = [build_subtree(c) for c in child_keys]

            m_ids = set(self._meters_by_dt.get(key, []))
            norm_c = normalize_code(base_node.code)
            if norm_c:
                m_ids.update(self._meters_by_dt.get(norm_c, []))

            p_key = effective_parent.get(key)
            return TreeNode(
                node_path=base_node.node_path or key,
                code=norm_c or base_node.code or key.split("/")[-1],
                name=base_node.name,
                level=base_node.level,
                parent_path=p_key,
                parent_code=p_key.split("/")[-1] if p_key else None,
                quality_flags=flags_by_key[key],
                children=children,
                meter_ids=sorted(m_ids),
            )

        root_keys = sorted(children_by_parent.get(None, []))
        tree = [build_subtree(rk) for rk in root_keys]

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
