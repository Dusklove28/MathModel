"""Deterministic fixed-mapping intra-core ordering for Problem 2.

Stage 3 keeps both the Problem-1 partition and the Stage-2 subgraph-to-core
assignment fixed.  It changes only the order of subgraphs already assigned to
each core.  The two bounded policies are screening heuristics; only the
unmodified official Scene-B evaluator may select a winner.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from solver_problem1 import GraphInfo
from evaluation_validation import validate_task_order
from solver_problem2 import MappingFeatures, build_mapping_features
from stub_multicore_cut_and_schedule import derive_multicore_plan


ORDER_POLICIES = ("release", "critical")


class Problem2OrderingError(RuntimeError):
    """A fixed-mapping ordering candidate could not be constructed."""


def subgraph_core_assignment(plan: Mapping[str, Any]) -> Dict[int, int]:
    """Return a validated subgraph-to-core assignment."""

    schedules = plan.get("core_schedules")
    if not isinstance(schedules, list) or any(
            not isinstance(order, list) for order in schedules):
        raise Problem2OrderingError("plan core_schedules must be a list of lists")
    assignment: Dict[int, int] = {}
    for core, order in enumerate(schedules):
        for raw_sg in order:
            sg = int(raw_sg)
            if sg in assignment:
                raise Problem2OrderingError(
                    "subgraph {} appears on multiple cores".format(sg))
            assignment[sg] = core
    return assignment


def _tensor_space(graph: GraphInfo, tid: int) -> str | None:
    space = graph.tensor_pos[tid]
    if space == "DDR":
        return "UB"
    return space if space in {"L1", "UB"} else None


def _edge_bytes(
    graph: GraphInfo,
    features: MappingFeatures,
    source: int,
    target: int,
) -> int:
    return sum(
        graph.tensor_size[tid]
        for tid in features.edge_tensor_ids.get((source, target), ())
    ) + int(features.edge_direct_bytes.get((source, target), 0))


def _ordering_trace(
    graph: GraphInfo,
    features: MappingFeatures,
    assignment: Mapping[int, int],
    policy: str,
    *,
    bandwidth: int,
    capacity: Mapping[str, int],
    cross_core_delay: int,
) -> Tuple[Tuple[int, ...], List[Dict[str, Any]], Dict[str, Any]]:
    """Build one conservative global linear extension and project it per core."""

    if policy not in ORDER_POLICIES:
        raise Problem2OrderingError("unsupported ordering policy: {}".format(policy))
    indegree = {sg: len(features.preds[sg]) for sg in features.subgraph_ids}
    ready: Set[int] = {sg for sg in features.subgraph_ids if indegree[sg] == 0}
    scheduled: Set[int] = set()
    order: List[int] = []
    trace: List[Dict[str, Any]] = []
    finish: Dict[int, int] = {}
    core_available = [0] * (max(assignment.values(), default=-1) + 1)
    ddr_available = 0

    local_consumers: Dict[Tuple[int, int], Set[int]] = defaultdict(set)
    global_consumers: Dict[int, Set[int]] = defaultdict(set)
    producer_cores: Dict[int, Set[int]] = defaultdict(set)
    for tid in graph.tensor_size:
        for sg in features.tensor_consumer_subgraphs.get(tid, ()):
            local_consumers[(assignment[sg], tid)].add(sg)
            global_consumers[tid].add(sg)
        for sg in features.tensor_producer_subgraphs.get(tid, ()):
            producer_cores[tid].add(assignment[sg])

    live: List[Set[int]] = [set() for _ in core_available]
    peak = [{"L1": 0, "UB": 0} for _ in core_available]
    graph_inputs = set(features.graph_input_tensors)
    graph_outputs = set(features.graph_output_tensors)

    def remaining_local(core: int, tid: int, chosen: int | None = None) -> Set[int]:
        values = local_consumers.get((core, tid), set()) - scheduled
        if chosen is not None:
            values.discard(chosen)
        return values

    def remaining_global(tid: int, chosen: int | None = None) -> Set[int]:
        values = global_consumers.get(tid, set()) - scheduled
        if chosen is not None:
            values.discard(chosen)
        return values

    def releasable(core: int, tid: int, chosen: int | None = None) -> bool:
        if remaining_local(core, tid, chosen):
            return False
        if core in producer_cores.get(tid, set()) and remaining_global(tid, chosen):
            return False
        return True

    def candidate_stats(sg: int) -> Dict[str, Any]:
        core = assignment[sg]
        inputs = set(features.input_tensors[sg])
        outputs = set(features.output_tensors[sg])
        added = {tid for tid in inputs | outputs if tid not in live[core]}
        considered = live[core] | added
        released = {
            tid for tid in considered if releasable(core, tid, sg)
        }
        long_lived = added - released
        projected_live = considered - released
        projected_bytes = {"L1": 0, "UB": 0}
        for tid in projected_live:
            space = _tensor_space(graph, tid)
            if space is not None:
                projected_bytes[space] += graph.tensor_size[tid]
        projected_peak = {
            space: max(peak[core][space], projected_bytes[space])
            for space in ("L1", "UB")
        }
        max_excess = max(
            projected_peak[space] - int(capacity[space])
            for space in ("L1", "UB")
        )
        max_excess = max(0, max_excess)

        dependency_ready = 0
        incoming_cross_bytes = 0
        incoming_cross_count = 0
        for pred in features.preds[sg]:
            delay = 0
            if assignment[pred] != core:
                payload = _edge_bytes(graph, features, pred, sg)
                incoming_cross_bytes += payload
                incoming_cross_count += 1
                delay = cross_core_delay + int(math.ceil(
                    payload / max(1, bandwidth)))
            dependency_ready = max(dependency_ready, finish[pred] + delay)
        pipe_start = max(core_available[core], dependency_ready)

        input_read_bytes = sum(
            graph.tensor_size[tid] for tid in inputs
            if tid in graph_inputs and tid not in live[core]
        )
        output_write_bytes = sum(
            graph.tensor_size[tid] for tid in outputs if tid in graph_outputs
        )
        ddr_bytes = input_read_bytes + 2 * incoming_cross_bytes + output_write_bytes
        ddr_cycles = int(math.ceil(ddr_bytes / max(1, bandwidth)))
        ddr_start = max(ddr_available, dependency_ready)
        estimated_start = max(pipe_start, ddr_start + ddr_cycles)
        estimated_finish = estimated_start + features.workload[sg]

        unlock_score = 0
        cross_consumer_bytes = 0
        for successor in features.succs[sg]:
            if all(pred in scheduled or pred == sg
                   for pred in features.preds[successor]):
                weight = features.rank[successor]
                if assignment[successor] != core:
                    payload = _edge_bytes(graph, features, sg, successor)
                    cross_consumer_bytes += payload
                    weight += cross_core_delay + int(math.ceil(
                        payload / max(1, bandwidth)))
                unlock_score += weight

        released_bytes = sum(graph.tensor_size[tid] for tid in released)
        long_lived_bytes = sum(graph.tensor_size[tid] for tid in long_lived)
        projected_live_bytes = sum(projected_bytes.values())
        pipe_idle = max(0, estimated_start - core_available[core])
        if policy == "release":
            key = (
                max_excess,
                projected_live_bytes,
                long_lived_bytes,
                -released_bytes,
                -unlock_score,
                -features.rank[sg],
                pipe_idle,
                estimated_finish,
                core,
                sg,
            )
        else:
            key = (
                -features.rank[sg],
                -unlock_score,
                estimated_finish,
                max_excess,
                projected_live_bytes,
                long_lived_bytes,
                -released_bytes,
                pipe_idle,
                core,
                sg,
            )
        return {
            "key": key,
            "core": core,
            "inputs": inputs,
            "outputs": outputs,
            "added": added,
            "released": released,
            "projected_live": projected_live,
            "projected_bytes": projected_bytes,
            "projected_peak": projected_peak,
            "max_excess": max_excess,
            "released_bytes": released_bytes,
            "long_lived_bytes": long_lived_bytes,
            "unlock_score": unlock_score,
            "cross_consumer_bytes": cross_consumer_bytes,
            "incoming_cross_bytes": incoming_cross_bytes,
            "incoming_cross_count": incoming_cross_count,
            "input_read_bytes": input_read_bytes,
            "output_write_bytes": output_write_bytes,
            "ddr_bytes": ddr_bytes,
            "ddr_cycles": ddr_cycles,
            "dependency_ready": dependency_ready,
            "estimated_start": estimated_start,
            "estimated_finish": estimated_finish,
            "pipe_idle": pipe_idle,
        }

    while ready:
        candidates = {sg: candidate_stats(sg) for sg in ready}
        chosen = min(ready, key=lambda sg: candidates[sg]["key"])
        stats = candidates[chosen]
        core = stats["core"]
        ready.remove(chosen)
        order.append(chosen)

        live[core].update(stats["added"])
        scheduled.add(chosen)
        # A remote consumer becoming scheduled can release a source-core copy,
        # so reclaim across all cores after each global step.
        for current_core in range(len(live)):
            live[current_core] = {
                tid for tid in live[current_core]
                if not releasable(current_core, tid)
            }
            live_bytes = {"L1": 0, "UB": 0}
            for tid in live[current_core]:
                space = _tensor_space(graph, tid)
                if space is not None:
                    live_bytes[space] += graph.tensor_size[tid]
            for space in ("L1", "UB"):
                peak[current_core][space] = max(
                    peak[current_core][space], live_bytes[space])

        finish[chosen] = stats["estimated_finish"]
        core_available[core] = stats["estimated_finish"]
        ddr_available = max(ddr_available, stats["dependency_ready"]) + stats[
            "ddr_cycles"]
        trace.append({
            "step": len(order),
            "subgraph": chosen,
            "core": core,
            "rank_cycles": features.rank[chosen],
            "released_bytes": stats["released_bytes"],
            "new_long_lived_bytes": stats["long_lived_bytes"],
            "projected_live_bytes": sum(stats["projected_bytes"].values()),
            "projected_peak_excess_bytes": stats["max_excess"],
            "successor_unlock_score": stats["unlock_score"],
            "cross_consumer_bytes": stats["cross_consumer_bytes"],
            "incoming_cross_bytes": stats["incoming_cross_bytes"],
            "incoming_cross_count": stats["incoming_cross_count"],
            "input_read_bytes": stats["input_read_bytes"],
            "output_write_bytes": stats["output_write_bytes"],
            "ddr_work_bytes": stats["ddr_bytes"],
            "estimated_dependency_ready": stats["dependency_ready"],
            "estimated_start": stats["estimated_start"],
            "estimated_finish": stats["estimated_finish"],
            "estimated_pipe_idle": stats["pipe_idle"],
        })
        for successor in features.succs[chosen]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.add(successor)

    if len(order) != len(features.subgraph_ids):
        raise Problem2OrderingError("fixed subgraph graph contains a cycle")
    return tuple(order), trace, {
        "estimated_finish_cycles": max(finish.values(), default=0),
        "estimated_shared_ddr_finish_cycles": ddr_available,
        "memory_peak_by_core_proxy": {
            str(core): values for core, values in enumerate(peak)
        },
    }


def generate_scene_b_order_candidates(
    graph_json: Mapping[str, Any],
    fixed_plan: Mapping[str, Any],
    *,
    bandwidth: int = 60,
    capacity: Mapping[str, int] | None = None,
    cross_core_delay: int = 500,
    policies: Sequence[str] = ORDER_POLICIES,
) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Return at most two deterministic orderings for one fixed mapping."""

    selected = tuple(policies)
    if not selected or len(set(selected)) != len(selected):
        raise Problem2OrderingError("ordering policies must be non-empty and unique")
    unknown = sorted(set(selected) - set(ORDER_POLICIES))
    if unknown:
        raise Problem2OrderingError(
            "unsupported ordering policies: {}".format(unknown))
    if len(selected) > 2:
        raise Problem2OrderingError("Stage-3 Gate A permits at most two policies")

    graph = GraphInfo.from_graph(graph_json)
    view = derive_multicore_plan(graph_json, fixed_plan)
    validate_task_order(view)
    features = build_mapping_features(
        graph_json,
        graph,
        fixed_plan,
        bandwidth=bandwidth,
        cross_core_delay=cross_core_delay,
    )
    assignment = subgraph_core_assignment(fixed_plan)
    if set(assignment) != set(features.subgraph_ids):
        raise Problem2OrderingError(
            "fixed schedules do not cover every subgraph exactly once")
    capacity = dict(capacity or {"L1": 524288, "UB": 131072})
    num_cores = len(fixed_plan["core_schedules"])
    results: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    for policy in selected:
        global_order, trace, proxy = _ordering_trace(
            graph,
            features,
            assignment,
            policy,
            bandwidth=bandwidth,
            capacity=capacity,
            cross_core_delay=cross_core_delay,
        )
        schedules: List[List[int]] = [[] for _ in range(num_cores)]
        for sg in global_order:
            schedules[assignment[sg]].append(sg)
        plan = {
            "node_to_subgraph": copy.deepcopy(
                dict(fixed_plan["node_to_subgraph"])),
            "core_schedules": schedules,
        }
        candidate_assignment = subgraph_core_assignment(plan)
        if candidate_assignment != assignment:
            raise Problem2OrderingError("ordering candidate changed core mapping")
        derived = derive_multicore_plan(graph_json, plan)
        validate_task_order(derived)
        diagnostics = {
            "policy": policy,
            "fixed_partition": True,
            "fixed_mapping": True,
            "only_intra_core_order_changed": True,
            "order_changed": schedules != fixed_plan["core_schedules"],
            "fixed_core_by_subgraph": {
                str(sg): assignment[sg] for sg in sorted(assignment)
            },
            "original_core_schedules": copy.deepcopy(
                fixed_plan["core_schedules"]),
            "candidate_core_schedules": copy.deepcopy(schedules),
            "global_linear_extension": list(global_order),
            "selection_trace": trace,
            "proxy": proxy,
        }
        results[policy] = (plan, diagnostics)
    return results

