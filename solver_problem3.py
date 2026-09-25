"""Two bounded, deterministic L2 reuse-window orderings on a fixed mapping.

The proxy models issue-time lookup, completion-time FIFO insertion, separate
DDR/L2 bandwidth, cross-core release delay, and rough on-chip pressure.
Only the supplied Problem-3 evaluator decides which candidate is best.
"""

from __future__ import annotations

import copy
import math
from collections import OrderedDict, defaultdict
from typing import Any, Mapping

from solver_problem1 import GraphInfo
from solver_problem2 import build_mapping_features
from solver_problem2_stage3 import subgraph_core_assignment
from evaluation_validation import validate_task_order
from stub_multicore_cut_and_schedule import derive_multicore_plan


POLICIES = ("reuse_window", "balanced_window")


class Problem3OrderingError(RuntimeError):
    """The frozen partition/mapping cannot yield a legal ordering."""


def _fifo_at(fill_events, at_time: int, cache_capacity: int):
    entries: OrderedDict[int, int] = OrderedDict()
    used = 0
    for finished, serial, tid, size in sorted(fill_events):
        if finished > at_time or size > cache_capacity or tid in entries:
            continue
        while entries and used + size > cache_capacity:
            _, old_size = entries.popitem(last=False)
            used -= old_size
        entries[tid] = size
        used += size
    return entries, used


def generate_reuse_window_candidates(
    graph_json: Mapping[str, Any],
    fixed_plan: Mapping[str, Any],
    *,
    bandwidth: int,
    capacity: Mapping[str, int],
    cross_core_delay: int,
    cache_capacity_bytes: int,
    cache_bandwidth_bytes_per_cycle: int,
) -> dict[str, tuple[dict, dict]]:
    """Return at most two legal plans; partition and core assignment stay fixed."""
    if bandwidth <= 0 or cache_capacity_bytes < 0 or cache_bandwidth_bytes_per_cycle <= 0:
        raise ValueError("bandwidth/cache configuration must be positive")
    graph = GraphInfo.from_graph(graph_json)
    view = derive_multicore_plan(graph_json, fixed_plan)
    validate_task_order(view)
    features = build_mapping_features(
        graph_json, graph, fixed_plan,
        bandwidth=bandwidth, cross_core_delay=cross_core_delay,
    )
    assignment = subgraph_core_assignment(fixed_plan)
    if set(assignment) != set(features.subgraph_ids):
        raise Problem3OrderingError("fixed mapping does not cover every subgraph")
    num_cores = len(fixed_plan["core_schedules"])
    graph_inputs = set(features.graph_input_tensors)
    graph_outputs = set(features.graph_output_tensors)
    results = {}
    for policy in POLICIES:
        indegree = {sg: len(features.preds[sg]) for sg in features.subgraph_ids}
        ready = {sg for sg in features.subgraph_ids if indegree[sg] == 0}
        ordered, scheduled = [], set()
        core_available = [0] * num_cores
        finish = {}
        ddr_available = l2_available = 0
        read_on_core = set()
        cross_read = set()
        fill_events = []
        live = [set() for _ in range(num_cores)]
        local_consumers = defaultdict(set)
        producer_cores = defaultdict(set)
        global_consumers = defaultdict(set)
        for tid in graph.tensor_size:
            for sg in features.tensor_consumer_subgraphs.get(tid, ()):
                local_consumers[(assignment[sg], tid)].add(sg)
                global_consumers[tid].add(sg)
            for sg in features.tensor_producer_subgraphs.get(tid, ()):
                producer_cores[tid].add(assignment[sg])
        trace = []

        def still_live(core: int, tid: int, chosen: int | None = None) -> bool:
            remaining = local_consumers[(core, tid)] - scheduled
            if chosen is not None:
                remaining.discard(chosen)
            if remaining:
                return True
            if core in producer_cores[tid]:
                remote = global_consumers[tid] - scheduled
                if chosen is not None:
                    remote.discard(chosen)
                return bool(remote)
            return False

        def candidate(sg: int) -> dict:
            core = assignment[sg]
            incoming_tids = []
            cross_output_bytes = direct_cross_bytes = 0
            dependency_ready = 0
            for pred in features.preds[sg]:
                if assignment[pred] == core:
                    dependency_ready = max(dependency_ready, finish[pred])
                    continue
                pair = (pred, sg)
                tensor_ids = features.edge_tensor_ids.get(pair, ())
                edge_bytes = sum(graph.tensor_size[tid] for tid in tensor_ids)
                direct_bytes = features.edge_direct_bytes.get(pair, 0)
                cross_output_bytes += edge_bytes
                direct_cross_bytes += direct_bytes
                dependency_ready = max(
                    dependency_ready,
                    finish[pred] + cross_core_delay
                    + math.ceil((edge_bytes + direct_bytes) / bandwidth),
                )
                for tid in tensor_ids:
                    key = (assignment[pred], core, tid)
                    if key not in cross_read:
                        incoming_tids.append((tid, key))
            for tid in features.input_tensors[sg]:
                key = (core, tid)
                if tid in graph_inputs and key not in read_on_core:
                    incoming_tids.append((tid, key))
            # Avoid duplicate COPY_IN estimates from multiple predecessor SGs.
            unique_reads = {}
            for tid, key in incoming_tids:
                unique_reads[key] = tid
            issue_time = max(core_available[core], dependency_ready)
            entries, used = _fifo_at(fill_events, issue_time, cache_capacity_bytes)
            hits, misses, hit_bytes, miss_bytes = [], [], 0, 0
            for key, tid in sorted(unique_reads.items(), key=lambda item: str(item[0])):
                size = graph.tensor_size[tid]
                if size > 0 and tid in entries:
                    hits.append((tid, key, size))
                    hit_bytes += size
                else:
                    misses.append((tid, key, size))
                    miss_bytes += size
            output_bytes = sum(
                graph.tensor_size[tid]
                for tid in features.output_tensors[sg]
                if tid in graph_outputs
            )
            projected = live[core] | set(features.input_tensors[sg]) | set(
                features.output_tensors[sg])
            projected = {tid for tid in projected if still_live(core, tid, sg)}
            live_bytes = {"L1": 0, "UB": 0}
            for tid in projected:
                pos = graph.tensor_pos[tid]
                space = "UB" if pos == "DDR" else pos
                if space in live_bytes:
                    live_bytes[space] += graph.tensor_size[tid]
            spill_excess = sum(max(0, live_bytes[space] - capacity[space])
                               for space in ("L1", "UB"))
            # Cross-core COPY_OUT always consumes DDR; target COPY_IN is
            # counted above as hit or miss. Direct edges require both legs.
            ddr_bytes = (miss_bytes + cross_output_bytes +
                         2 * direct_cross_bytes + output_bytes +
                         2 * spill_excess)
            ddr_finish = max(issue_time, ddr_available) + math.ceil(
                ddr_bytes / bandwidth)
            l2_finish = max(issue_time, l2_available) + math.ceil(
                hit_bytes / cache_bandwidth_bytes_per_cycle)
            estimated_finish = max(
                issue_time + features.workload[sg], ddr_finish, l2_finish)
            reuse_credit = hit_bytes * (
                1 / bandwidth - 1 / cache_bandwidth_bytes_per_cycle)
            # A hit on an older FIFO entry is more urgent, because later
            # insertions evict from the front and hits never refresh order.
            fifo_urgency = sum(
                (len(entries) - list(entries).index(tid)) * size
                for tid, _, size in hits
            ) / max(1, cache_capacity_bytes)
            critical = features.rank[sg]
            if policy == "reuse_window":
                score = (estimated_finish - 0.50 * critical
                         - 2.0 * reuse_credit - fifo_urgency)
            else:
                score = (estimated_finish - 0.85 * critical
                         - 0.70 * reuse_credit - 0.25 * fifo_urgency)
            key = (spill_excess > 0, score, estimated_finish,
                   -critical, core, sg)
            return {
                "key": key, "core": core, "issue_time": issue_time,
                "finish": estimated_finish, "ddr_finish": ddr_finish,
                "l2_finish": l2_finish, "ddr_bytes": ddr_bytes,
                "hit_bytes": hit_bytes, "miss_bytes": miss_bytes,
                "spill_excess_bytes": spill_excess,
                "projected_live": projected,
                "hits": hits, "misses": misses,
                "new_read_keys": set(unique_reads),
                "cache_used_at_issue": used,
            }

        while ready:
            stats_by_sg = {sg: candidate(sg) for sg in ready}
            chosen = min(ready, key=lambda sg: stats_by_sg[sg]["key"])
            stats = stats_by_sg[chosen]
            core = stats["core"]
            ready.remove(chosen)
            scheduled.add(chosen)
            ordered.append(chosen)
            finish[chosen] = stats["finish"]
            core_available[core] = stats["finish"]
            ddr_available = stats["ddr_finish"]
            l2_available = stats["l2_finish"]
            for tid, key, size in stats["misses"]:
                fill_events.append((stats["ddr_finish"], len(fill_events),
                                    tid, size))
            for key in stats["new_read_keys"]:
                if len(key) == 2:
                    read_on_core.add(key)
                else:
                    cross_read.add(key)
            live[core] = set(stats["projected_live"])
            for current_core in range(num_cores):
                live[current_core] = {
                    tid for tid in live[current_core]
                    if still_live(current_core, tid)
                }
            trace.append({
                "subgraph": chosen, "core": core,
                "estimated_issue": stats["issue_time"],
                "estimated_finish": stats["finish"],
                "estimated_ddr_bytes": stats["ddr_bytes"],
                "estimated_l2_hit_bytes": stats["hit_bytes"],
                "estimated_l2_miss_bytes": stats["miss_bytes"],
                "estimated_spill_excess_bytes": stats["spill_excess_bytes"],
                "cache_used_at_issue": stats["cache_used_at_issue"],
            })
            for successor in features.succs[chosen]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.add(successor)
        if len(ordered) != len(features.subgraph_ids):
            raise Problem3OrderingError("subgraph dependency graph contains a cycle")
        schedules = [[] for _ in range(num_cores)]
        for sg in ordered:
            schedules[assignment[sg]].append(sg)
        plan = {
            "node_to_subgraph": copy.deepcopy(fixed_plan["node_to_subgraph"]),
            "core_schedules": schedules,
        }
        if subgraph_core_assignment(plan) != assignment:
            raise Problem3OrderingError("candidate changed core assignment")
        validate_task_order(derive_multicore_plan(graph_json, plan))
        diagnostics = {
            "policy": policy,
            "fixed_partition": True, "fixed_mapping": True,
            "order_changed": schedules != fixed_plan["core_schedules"],
            "estimated_finish_cycles": max(finish.values(), default=0),
            "estimated_l2_hit_bytes": sum(item["estimated_l2_hit_bytes"]
                                          for item in trace),
            "estimated_l2_miss_bytes": sum(item["estimated_l2_miss_bytes"]
                                           for item in trace),
            "estimated_ddr_bytes": sum(item["estimated_ddr_bytes"]
                                       for item in trace),
            "trace": trace,
        }
        results[policy] = (plan, diagnostics)
    return results
