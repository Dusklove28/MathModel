"""Deterministic fixed-partition core mapping for Problem 2 (Scene B).

The module deliberately does not score final plans.  It keeps a Problem-1
partition fixed, proposes a small number of mappings, and uses a Scene-B-aware
proxy only to rank bounded local moves.  Every returned plan is checked by the
unmodified official plan and task-order validators; callers must still use the
official Scene-B evaluator to select a winner.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from solver_problem1 import GraphInfo
from evaluation_validation import validate_task_order
from stub_multicore_cut_and_schedule import derive_multicore_plan


MAPPING_POLICIES = ("critical", "locality", "memory")


class Problem2MappingError(RuntimeError):
    """A fixed-partition mapping candidate could not be constructed."""


@dataclass(frozen=True)
class MappingFeatures:
    subgraph_ids: Tuple[int, ...]
    nodes: Mapping[int, Tuple[int, ...]]
    preds: Mapping[int, Tuple[int, ...]]
    succs: Mapping[int, Tuple[int, ...]]
    global_order: Tuple[int, ...]
    pipe_m_cycles: Mapping[int, int]
    pipe_v_cycles: Mapping[int, int]
    workload: Mapping[int, int]
    rank: Mapping[int, int]
    edge_tensor_ids: Mapping[Tuple[int, int], Tuple[int, ...]]
    edge_direct_bytes: Mapping[Tuple[int, int], int]
    input_tensors: Mapping[int, Tuple[int, ...]]
    output_tensors: Mapping[int, Tuple[int, ...]]
    tensor_producer_subgraphs: Mapping[int, Tuple[int, ...]]
    tensor_consumer_subgraphs: Mapping[int, Tuple[int, ...]]
    graph_input_tensors: Tuple[int, ...]
    graph_output_tensors: Tuple[int, ...]
    direct_edges: Tuple[Tuple[int, int, int], ...]


def _ready_order(
    subgraph_ids: Sequence[int],
    preds: Mapping[int, Sequence[int]],
    succs: Mapping[int, Sequence[int]],
    rank: Mapping[int, int],
) -> Tuple[int, ...]:
    indegree = {sg: len(preds[sg]) for sg in subgraph_ids}
    ready = {sg for sg in subgraph_ids if indegree[sg] == 0}
    order: List[int] = []
    while ready:
        sg = min(ready, key=lambda value: (-rank[value], value))
        ready.remove(sg)
        order.append(sg)
        for successor in succs[sg]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.add(successor)
    if len(order) != len(subgraph_ids):
        raise Problem2MappingError("fixed subgraph graph contains a cycle")
    return tuple(order)


def build_mapping_features(
    graph_json: Mapping[str, Any],
    graph: GraphInfo,
    base_plan: Mapping[str, Any],
    *,
    bandwidth: int = 60,
    cross_core_delay: int = 500,
) -> MappingFeatures:
    """Build immutable Scene-B features for one fixed partition."""

    view = derive_multicore_plan(graph_json, base_plan)
    validate_task_order(view)
    subgraph_ids = tuple(view["subgraph_ids"])
    nodes = {
        sg: tuple(view["nodes_by_subgraph"][sg]) for sg in subgraph_ids
    }
    preds = {
        sg: tuple(sorted(view["subgraph_preds"][sg])) for sg in subgraph_ids
    }
    succs = {
        sg: tuple(sorted(view["subgraph_succs"][sg])) for sg in subgraph_ids
    }
    node_to_sg = dict(view["mapping"])

    pipe_m = {
        sg: sum(graph.pipe_m_cycles[node] for node in nodes[sg])
        for sg in subgraph_ids
    }
    pipe_v = {
        sg: sum(graph.pipe_v_cycles[node] for node in nodes[sg])
        for sg in subgraph_ids
    }
    workload = {sg: max(pipe_m[sg], pipe_v[sg]) for sg in subgraph_ids}

    edge_tensors: Dict[Tuple[int, int], Set[int]] = defaultdict(set)
    edge_direct: Dict[Tuple[int, int], int] = defaultdict(int)
    for source in graph.topo_order:
        source_sg = node_to_sg[source]
        for target in graph.succs[source]:
            target_sg = node_to_sg[target]
            if source_sg == target_sg:
                continue
            pair = (source_sg, target_sg)
            edge_tensors[pair].update(
                graph.dependency_tensors.get((source, target), ()))
            edge_direct[pair] += int(
                graph.dependency_direct_bytes.get((source, target), 0))

    edge_bytes = {
        pair: sum(graph.tensor_size[tid] for tid in tids)
        + edge_direct.get(pair, 0)
        for pair, tids in edge_tensors.items()
    }
    for source in subgraph_ids:
        for target in succs[source]:
            edge_bytes.setdefault((source, target), edge_direct[(source, target)])

    rank: Dict[int, int] = {}
    # Any deterministic topological order is enough for the reverse DP.
    indegree = {sg: len(preds[sg]) for sg in subgraph_ids}
    ready = sorted(sg for sg in subgraph_ids if indegree[sg] == 0)
    topo: List[int] = []
    while ready:
        sg = ready.pop(0)
        topo.append(sg)
        for target in succs[sg]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    for sg in reversed(topo):
        rank[sg] = workload[sg] + max(
            (cross_core_delay + int(math.ceil(
                edge_bytes.get((sg, target), 0) / max(1, bandwidth)))
             + rank[target]
             for target in succs[sg]),
            default=0,
        )
    # Use a single linear extension that also respects every original per-core
    # adjacency.  Projecting this order back onto the original assignment then
    # reproduces the original core schedules exactly.  Consequently Stage 2
    # changes mapping only; ready-list reordering remains a separate Stage-3
    # ablation.
    order_pred_sets = {sg: set(preds[sg]) for sg in subgraph_ids}
    order_succ_sets = {sg: set(succs[sg]) for sg in subgraph_ids}
    for order in base_plan["core_schedules"]:
        for source, target in zip(order, order[1:]):
            order_succ_sets[source].add(target)
            order_pred_sets[target].add(source)
    global_order = _ready_order(
        subgraph_ids,
        {sg: tuple(sorted(order_pred_sets[sg])) for sg in subgraph_ids},
        {sg: tuple(sorted(order_succ_sets[sg])) for sg in subgraph_ids},
        rank,
    )

    eligible = set(graph.eligible_ops)
    producer_sgs: Dict[int, Set[int]] = defaultdict(set)
    consumer_sgs: Dict[int, Set[int]] = defaultdict(set)
    for tid, producers in graph.tensor_producer.items():
        for op in producers:
            if op in eligible:
                producer_sgs[tid].add(node_to_sg[op])
    for tid, consumers in graph.tensor_consumers.items():
        for op in consumers:
            if op in eligible:
                consumer_sgs[tid].add(node_to_sg[op])

    op_type = {int(op["id"]): str(op.get("op", ""))
               for op in graph_json["ops"]}
    graph_inputs: List[int] = []
    graph_outputs: List[int] = []
    for tid in sorted(graph.tensor_size):
        producers = [op for op in graph.tensor_producer.get(tid, ())
                     if op in eligible]
        consumers = [op for op in graph.tensor_consumers.get(tid, ())
                     if op in eligible]
        if consumers and not producers:
            graph_inputs.append(tid)
        has_copy_out = any(
            op_type.get(op) == "COPY_OUT"
            for op in graph.tensor_consumers.get(tid, ())
        )
        if producers and (has_copy_out or not consumers):
            graph_outputs.append(tid)

    input_by_sg: Dict[int, Set[int]] = {sg: set() for sg in subgraph_ids}
    output_by_sg: Dict[int, Set[int]] = {sg: set() for sg in subgraph_ids}
    for sg, sg_nodes in nodes.items():
        for node in sg_nodes:
            input_by_sg[sg].update(graph.op_input_tensors[node])
            output_by_sg[sg].update(graph.op_output_tensors[node])

    direct_edges: List[Tuple[int, int, int]] = []
    op_ids = {int(op["id"]) for op in graph_json["ops"]}
    for edge in graph_json["edges"]:
        source, target = edge["source"], edge["target"]
        if source in eligible and target in eligible:
            size = edge.get("data_size", 0)
            size = int(size) if isinstance(size, (int, float)) else 0
            direct_edges.append((node_to_sg[source], node_to_sg[target], max(0, size)))

    return MappingFeatures(
        subgraph_ids=subgraph_ids,
        nodes=nodes,
        preds=preds,
        succs=succs,
        global_order=global_order,
        pipe_m_cycles=pipe_m,
        pipe_v_cycles=pipe_v,
        workload=workload,
        rank=rank,
        edge_tensor_ids={pair: tuple(sorted(tids))
                         for pair, tids in edge_tensors.items()},
        edge_direct_bytes=dict(edge_direct),
        input_tensors={sg: tuple(sorted(values))
                       for sg, values in input_by_sg.items()},
        output_tensors={sg: tuple(sorted(values))
                        for sg, values in output_by_sg.items()},
        tensor_producer_subgraphs={
            tid: tuple(sorted(producer_sgs.get(tid, ())))
            for tid in graph.tensor_size
        },
        tensor_consumer_subgraphs={
            tid: tuple(sorted(consumer_sgs.get(tid, ())))
            for tid in graph.tensor_size
        },
        graph_input_tensors=tuple(graph_inputs),
        graph_output_tensors=tuple(graph_outputs),
        direct_edges=tuple(direct_edges),
    )


def _schedules_from_assignment(
    assignment: Mapping[int, int],
    global_order: Sequence[int],
    num_cores: int,
) -> List[List[int]]:
    schedules: List[List[int]] = [[] for _ in range(num_cores)]
    for sg in global_order:
        schedules[assignment[sg]].append(sg)
    return schedules


def _base_assignment(base_plan: Mapping[str, Any]) -> Dict[int, int]:
    return {
        sg: core
        for core, order in enumerate(base_plan["core_schedules"])
        for sg in order
    }


def _tensor_core_sets(
    features: MappingFeatures,
    assignment: Mapping[int, int],
    tid: int,
) -> Tuple[Set[int], Set[int]]:
    producer_cores = {
        assignment[sg] for sg in features.tensor_producer_subgraphs[tid]
    }
    consumer_cores = {
        assignment[sg] for sg in features.tensor_consumer_subgraphs[tid]
    }
    return producer_cores, consumer_cores


def scene_b_mapping_proxy(
    graph: GraphInfo,
    features: MappingFeatures,
    assignment: Mapping[int, int],
    num_cores: int,
    *,
    bandwidth: int = 60,
    capacity: Mapping[str, int] | None = None,
    cross_core_delay: int = 500,
) -> Dict[str, Any]:
    """Return a deterministic screening proxy with official transfer semantics.

    It models input reads per consumer core, tensor transfer de-duplication by
    (tensor, source core, target core), direct op-op edges individually, graph
    output writes, critical dependency waits, per-pipe load, shared-DDR work,
    and conservative local tensor lifetimes.  It is not an official score.
    """

    if set(assignment) != set(features.subgraph_ids):
        raise Problem2MappingError("assignment does not cover every subgraph")
    if any(core < 0 or core >= num_cores for core in assignment.values()):
        raise Problem2MappingError("assignment contains an invalid core")
    capacity = dict(capacity or {"L1": 524288, "UB": 131072})
    schedules = _schedules_from_assignment(
        assignment, features.global_order, num_cores)
    position = {
        (core, sg): index
        for core, order in enumerate(schedules)
        for index, sg in enumerate(order)
    }

    pipe_m = [0] * num_cores
    pipe_v = [0] * num_cores
    for sg in features.subgraph_ids:
        core = assignment[sg]
        pipe_m[core] += features.pipe_m_cycles[sg]
        pipe_v[core] += features.pipe_v_cycles[sg]

    input_bytes = 0
    output_bytes = 0
    cross_tensor_bytes = 0
    cross_tensor_count = 0
    cross_pairs: Set[Tuple[int, int, int]] = set()
    touched_by_tensor: Dict[int, Set[int]] = defaultdict(set)
    for tid in sorted(graph.tensor_size):
        producer_cores, consumer_cores = _tensor_core_sets(
            features, assignment, tid)
        touched_by_tensor[tid].update(producer_cores | consumer_cores)
        if tid in features.graph_input_tensors:
            input_bytes += graph.tensor_size[tid] * len(consumer_cores)
        if tid in features.graph_output_tensors:
            output_bytes += graph.tensor_size[tid] * len(producer_cores)
        for source_core in producer_cores:
            for target_core in consumer_cores:
                if source_core == target_core:
                    continue
                key = (tid, source_core, target_core)
                if key not in cross_pairs:
                    cross_pairs.add(key)
                    cross_tensor_count += 1
                    cross_tensor_bytes += graph.tensor_size[tid]

    direct_cross_bytes = 0
    direct_cross_count = 0
    for source_sg, target_sg, size in features.direct_edges:
        if assignment[source_sg] != assignment[target_sg]:
            direct_cross_count += 1
            direct_cross_bytes += size

    cross_payload = cross_tensor_bytes + direct_cross_bytes
    transfer_count = cross_tensor_count + direct_cross_count
    scheduled_copy_bytes = (
        input_bytes + output_bytes + 2 * cross_payload
    )
    shared_ddr_cycles = int(math.ceil(scheduled_copy_bytes / max(1, bandwidth)))

    finish: Dict[int, int] = {}
    core_available = [0] * num_cores
    for sg in features.global_order:
        core = assignment[sg]
        ready = 0
        for pred in features.preds[sg]:
            delay = 0
            if assignment[pred] != core:
                pair = (pred, sg)
                tensor_bytes = sum(
                    graph.tensor_size[tid]
                    for tid in features.edge_tensor_ids.get(pair, ()))
                direct_bytes = features.edge_direct_bytes.get(pair, 0)
                delay = cross_core_delay + int(math.ceil(
                    (tensor_bytes + direct_bytes) / max(1, bandwidth)))
            ready = max(ready, finish[pred] + delay)
        start = max(core_available[core], ready)
        finish[sg] = start + features.workload[sg]
        core_available[core] = finish[sg]
    dependency_finish = max(finish.values(), default=0)

    memory_peak = {
        core: {"L1": 0, "UB": 0} for core in range(num_cores)
    }
    memory_events: Dict[Tuple[int, str], List[Tuple[int, int]]] = {}
    for core, order in enumerate(schedules):
        for space in ("L1", "UB"):
            memory_events[(core, space)] = [(0, 0)] * (len(order) + 1)
    # Difference arrays are represented as mutable integer lists below.
    diffs: Dict[Tuple[int, str], List[int]] = {
        key: [0] * (len(schedules[key[0]]) + 1) for key in memory_events
    }
    for tid in sorted(graph.tensor_size):
        tensor_space = graph.tensor_pos[tid]
        space = "UB" if tensor_space == "DDR" else tensor_space
        if space not in ("L1", "UB"):
            continue
        producer_sgs = features.tensor_producer_subgraphs[tid]
        consumer_sgs = features.tensor_consumer_subgraphs[tid]
        for core in sorted(touched_by_tensor.get(tid, ())):
            local_producers = [sg for sg in producer_sgs if assignment[sg] == core]
            local_consumers = [sg for sg in consumer_sgs if assignment[sg] == core]
            if not local_producers and not local_consumers:
                continue
            starts = [position[(core, sg)] for sg in local_producers]
            ends = [position[(core, sg)] for sg in local_consumers]
            start = min(starts) if starts else 0
            end = max(ends) if ends else max(0, len(schedules[core]) - 1)
            if tid in features.graph_output_tensors and local_producers:
                end = max(end, max(position[(core, sg)] for sg in local_producers))
            if end < start:
                start, end = end, start
            diffs[(core, space)][start] += graph.tensor_size[tid]
            if end + 1 < len(diffs[(core, space)]):
                diffs[(core, space)][end + 1] -= graph.tensor_size[tid]
    # Direct cross-core edges create an additional short-lived UB tensor.
    for source_sg, target_sg, size in features.direct_edges:
        source_core, target_core = assignment[source_sg], assignment[target_sg]
        if source_core == target_core or size <= 0:
            continue
        for core, sg in ((source_core, source_sg), (target_core, target_sg)):
            index = position[(core, sg)]
            diffs[(core, "UB")][index] += size
            if index + 1 < len(diffs[(core, "UB")]):
                diffs[(core, "UB")][index + 1] -= size
    for (core, space), values in diffs.items():
        current = 0
        peak = 0
        for value in values:
            current += value
            peak = max(peak, current)
        memory_peak[core][space] = peak
    memory_excess = {
        core: {
            space: max(0, memory_peak[core][space] - int(capacity[space]))
            for space in ("L1", "UB")
        }
        for core in range(num_cores)
    }
    max_memory_excess = max(
        (value for spaces in memory_excess.values() for value in spaces.values()),
        default=0,
    )
    total_memory_excess = sum(
        value for spaces in memory_excess.values() for value in spaces.values())
    pipe_bound = max(
        [max(pipe_m[core], pipe_v[core]) for core in range(num_cores)] + [0]
    )
    estimated_makespan = max(dependency_finish, pipe_bound, shared_ddr_cycles)
    return {
        "estimated_makespan_cycles": estimated_makespan,
        "dependency_finish_cycles": dependency_finish,
        "shared_ddr_cycles_lower_bound": shared_ddr_cycles,
        "pipe_bound_cycles": pipe_bound,
        "per_core_pipe_m_cycles": pipe_m,
        "per_core_pipe_v_cycles": pipe_v,
        "input_read_bytes": input_bytes,
        "output_write_bytes": output_bytes,
        "cross_tensor_payload_bytes": cross_tensor_bytes,
        "direct_cross_payload_bytes": direct_cross_bytes,
        "cross_payload_bytes": cross_payload,
        "cross_transfer_count": transfer_count,
        "scheduled_copy_bytes_proxy": scheduled_copy_bytes,
        "memory_peak_by_core_proxy": memory_peak,
        "memory_excess_by_core_proxy": memory_excess,
        "max_memory_excess_bytes": max_memory_excess,
        "total_memory_excess_bytes": total_memory_excess,
        "used_core_count": sum(bool(order) for order in schedules),
    }


def _objective(proxy: Mapping[str, Any], policy: str) -> Tuple[int, ...]:
    estimated = int(proxy["estimated_makespan_cycles"])
    cross = int(proxy["cross_payload_bytes"])
    copies = int(proxy["scheduled_copy_bytes_proxy"])
    max_excess = int(proxy["max_memory_excess_bytes"])
    total_excess = int(proxy["total_memory_excess_bytes"])
    transfer_count = int(proxy["cross_transfer_count"])
    if policy == "critical":
        return estimated, max_excess, cross, copies, transfer_count
    if policy == "locality":
        # Keep the estimated critical path primary so that the communication
        # policy cannot collapse all work onto one core merely to remove cuts.
        return estimated, cross, transfer_count, copies, max_excess
    if policy == "memory":
        return max_excess, total_excess, copies, estimated, cross
    raise Problem2MappingError("unknown mapping policy: {}".format(policy))


def _greedy_assignment(
    graph: GraphInfo,
    features: MappingFeatures,
    num_cores: int,
    policy: str,
    bandwidth: int,
    cross_core_delay: int,
) -> Dict[int, int]:
    """Construct one mapping using only already-scheduled predecessor data."""

    assignment: Dict[int, int] = {}
    core_available = [0] * num_cores
    pipe_m = [0] * num_cores
    pipe_v = [0] * num_cores
    finish: Dict[int, int] = {}
    seen_inputs: List[Set[int]] = [set() for _ in range(num_cores)]
    graph_inputs = set(features.graph_input_tensors)
    for sg in features.global_order:
        choices: List[Tuple[Tuple[int, ...], int, int]] = []
        for core in range(num_cores):
            cross_bytes = 0
            cross_count = 0
            ready = 0
            for pred in features.preds[sg]:
                pred_core = assignment[pred]
                pair = (pred, sg)
                pair_bytes = sum(
                    graph.tensor_size[tid]
                    for tid in features.edge_tensor_ids.get(pair, ()))
                pair_bytes += features.edge_direct_bytes.get(pair, 0)
                delay = 0
                if pred_core != core:
                    cross_bytes += pair_bytes
                    cross_count += 1
                    delay = cross_core_delay + int(math.ceil(
                        pair_bytes / max(1, bandwidth)))
                ready = max(ready, finish[pred] + delay)
            input_added = sum(
                graph.tensor_size[tid]
                for tid in features.input_tensors[sg]
                if tid in graph_inputs and tid not in seen_inputs[core]
            )
            projected_m = pipe_m[core] + features.pipe_m_cycles[sg]
            projected_v = pipe_v[core] + features.pipe_v_cycles[sg]
            load = max(projected_m, projected_v)
            eft = max(core_available[core], ready) + features.workload[sg]
            if policy == "critical":
                key = (eft, load, cross_bytes, input_added, cross_count, core)
            elif policy == "locality":
                key = (eft, cross_bytes, cross_count, input_added, load, core)
            elif policy == "memory":
                key = (eft, input_added + 2 * cross_bytes, cross_bytes,
                       load, cross_count, core)
            else:
                raise Problem2MappingError(
                    "unknown mapping policy: {}".format(policy))
            choices.append((key, core, eft))
        _, chosen, chosen_finish = min(choices)
        assignment[sg] = chosen
        finish[sg] = chosen_finish
        core_available[chosen] = chosen_finish
        pipe_m[chosen] += features.pipe_m_cycles[sg]
        pipe_v[chosen] += features.pipe_v_cycles[sg]
        seen_inputs[chosen].update(
            tid for tid in features.input_tensors[sg] if tid in graph_inputs)
    return assignment


def _bounded_relocation_search(
    graph: GraphInfo,
    features: MappingFeatures,
    initial: Mapping[int, int],
    num_cores: int,
    policy: str,
    *,
    bandwidth: int,
    capacity: Mapping[str, int],
    cross_core_delay: int,
    max_moves: int,
    search_width: int,
) -> Tuple[Dict[int, int], Dict[str, Any], List[Dict[str, Any]]]:
    assignment = dict(initial)
    proxy = scene_b_mapping_proxy(
        graph, features, assignment, num_cores,
        bandwidth=bandwidth, capacity=capacity,
        cross_core_delay=cross_core_delay,
    )
    objective = _objective(proxy, policy)
    trace: List[Dict[str, Any]] = []
    boundary = {
        sg for sg in features.subgraph_ids
        if features.preds[sg] or features.succs[sg]
    }
    move_candidates = tuple(sorted(
        boundary or set(features.subgraph_ids),
        key=lambda sg: (-features.rank[sg], -features.workload[sg], sg),
    )[:search_width])
    for iteration in range(max_moves):
        best: Tuple[Tuple[int, ...], int, int, Dict[str, Any]] | None = None
        for sg in move_candidates:
            old_core = assignment[sg]
            for new_core in range(num_cores):
                if new_core == old_core:
                    continue
                trial = dict(assignment)
                trial[sg] = new_core
                trial_proxy = scene_b_mapping_proxy(
                    graph, features, trial, num_cores,
                    bandwidth=bandwidth, capacity=capacity,
                    cross_core_delay=cross_core_delay,
                )
                trial_objective = _objective(trial_proxy, policy)
                candidate = (trial_objective, sg, new_core, trial_proxy)
                if trial_objective < objective and (
                        best is None or candidate[:3] < best[:3]):
                    best = candidate
        if best is None:
            break
        new_objective, sg, new_core, proxy = best
        old_core = assignment[sg]
        assignment[sg] = new_core
        trace.append({
            "iteration": iteration + 1,
            "subgraph": sg,
            "source_core": old_core,
            "target_core": new_core,
            "objective_before": list(objective),
            "objective_after": list(new_objective),
        })
        objective = new_objective
    return assignment, proxy, trace


def generate_scene_b_mapping_candidates(
    graph_json: Mapping[str, Any],
    base_plan: Mapping[str, Any],
    *,
    bandwidth: int = 60,
    capacity: Mapping[str, int] | None = None,
    cross_core_delay: int = 500,
    max_moves: int = 2,
    search_width: int = 12,
) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Return three deterministic mappings while preserving the partition."""

    if not isinstance(max_moves, int) or max_moves < 0:
        raise Problem2MappingError("max_moves must be a non-negative integer")
    if not isinstance(search_width, int) or search_width < 1:
        raise Problem2MappingError("search_width must be a positive integer")
    graph = GraphInfo.from_graph(graph_json)
    features = build_mapping_features(
        graph_json,
        graph,
        base_plan,
        bandwidth=bandwidth,
        cross_core_delay=cross_core_delay,
    )
    num_cores = len(base_plan["core_schedules"])
    capacity = dict(capacity or {"L1": 524288, "UB": 131072})
    base = _base_assignment(base_plan)
    if set(base) != set(features.subgraph_ids):
        raise Problem2MappingError("base schedules do not cover the partition")

    results: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    for policy in MAPPING_POLICIES:
        greedy = _greedy_assignment(
            graph, features, num_cores, policy, bandwidth, cross_core_delay)
        base_proxy = scene_b_mapping_proxy(
            graph, features, base, num_cores,
            bandwidth=bandwidth, capacity=capacity,
            cross_core_delay=cross_core_delay,
        )
        greedy_proxy = scene_b_mapping_proxy(
            graph, features, greedy, num_cores,
            bandwidth=bandwidth, capacity=capacity,
            cross_core_delay=cross_core_delay,
        )
        start_name, start = min(
            (("base", base), ("greedy", greedy)),
            key=lambda item: (_objective(
                base_proxy if item[0] == "base" else greedy_proxy, policy),
                item[0]),
        )
        assignment, final_proxy, trace = _bounded_relocation_search(
            graph, features, start, num_cores, policy,
            bandwidth=bandwidth,
            capacity=capacity,
            cross_core_delay=cross_core_delay,
            max_moves=max_moves,
            search_width=search_width,
        )
        plan = {
            "node_to_subgraph": copy.deepcopy(dict(base_plan["node_to_subgraph"])),
            "core_schedules": _schedules_from_assignment(
                assignment, features.global_order, num_cores),
        }
        view = derive_multicore_plan(graph_json, plan)
        validate_task_order(view)
        diagnostics = {
            "policy": policy,
            "fixed_partition": True,
            "start_mapping": start_name,
            "max_moves": max_moves,
            "search_width": search_width,
            "accepted_moves": len(trace),
            "move_trace": trace,
            "base_proxy": base_proxy,
            "greedy_proxy": greedy_proxy,
            "final_proxy": final_proxy,
            "base_core_by_subgraph": {
                str(sg): base[sg] for sg in sorted(base)
            },
            "final_core_by_subgraph": {
                str(sg): assignment[sg] for sg in sorted(assignment)
            },
        }
        results[policy] = (plan, diagnostics)
    return results

