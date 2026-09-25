"""At most two one-subgraph core moves for shared input branches.

This is a bounded development probe after fixed-mapping ordering has shown
benefit. Every proposal retains the original partition and is checked with
the official plan/task-order validators before official Problem-3 scoring.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from typing import Any, Mapping

from solver_problem1 import GraphInfo
from solver_problem2 import build_mapping_features, scene_b_mapping_proxy
from solver_problem2_stage3 import subgraph_core_assignment
from evaluation_validation import validate_task_order
from stub_multicore_cut_and_schedule import derive_multicore_plan


def generate_shared_input_moves(
    graph_json: Mapping[str, Any],
    base_plan: Mapping[str, Any],
    *,
    bandwidth: int,
    capacity: Mapping[str, int],
    cross_core_delay: int,
    cache_bandwidth: int,
) -> dict[str, tuple[dict, dict]]:
    graph = GraphInfo.from_graph(graph_json)
    view = derive_multicore_plan(graph_json, base_plan)
    validate_task_order(view)
    features = build_mapping_features(
        graph_json, graph, base_plan,
        bandwidth=bandwidth, cross_core_delay=cross_core_delay,
    )
    assignment = subgraph_core_assignment(base_plan)
    num_cores = len(base_plan["core_schedules"])
    candidates = []
    shared = []
    for tid in features.graph_input_tensors:
        consumers = features.tensor_consumer_subgraphs.get(tid, ())
        cores = {assignment[sg] for sg in consumers}
        if len(cores) > 1 and graph.tensor_size[tid] > 0:
            shared.append((tid, tuple(consumers), cores))
    # Bound proxy work independently of graph size; the largest shared reads
    # are the only ones considered for this optional mapping probe.
    shared.sort(key=lambda item: (-graph.tensor_size[item[0]], item[0]))
    move_pairs = set()
    for tid, consumers, cores in shared[:8]:
        for sg in consumers:
            source = assignment[sg]
            for destination in sorted(cores - {source}):
                move_pairs.add((sg, source, destination))
    for sg, source, destination in sorted(move_pairs)[:64]:
        changed = dict(assignment)
        changed[sg] = destination
        schedules = [[] for _ in range(num_cores)]
        for subgraph in features.global_order:
            schedules[changed[subgraph]].append(subgraph)
        plan = {
            "node_to_subgraph": copy.deepcopy(base_plan["node_to_subgraph"]),
            "core_schedules": schedules,
        }
        try:
            validate_task_order(derive_multicore_plan(graph_json, plan))
        except (ValueError, RuntimeError):
            continue
        before_cores = {
            tid: len({assignment[item]
                      for item in features.tensor_consumer_subgraphs.get(tid, ())})
            for tid, _, _ in shared
        }
        after_cores = {
            tid: len({changed[item]
                      for item in features.tensor_consumer_subgraphs.get(tid, ())})
            for tid, _, _ in shared
        }
        shared_read_bytes_saved = sum(
            max(0, before_cores[tid] - after_cores[tid]) * graph.tensor_size[tid]
            for tid, _, _ in shared
        )
        if shared_read_bytes_saved <= 0:
            continue
        proxy = scene_b_mapping_proxy(
            graph, features, changed, num_cores,
            bandwidth=bandwidth, capacity=capacity,
            cross_core_delay=cross_core_delay,
        )
        saved_l2_cycles = math.ceil(shared_read_bytes_saved / cache_bandwidth)
        info = {
            "moved_subgraph": sg, "source_core": source,
            "destination_core": destination,
            "shared_input_read_bytes_saved": shared_read_bytes_saved,
            "estimated_l2_read_cycles_saved": saved_l2_cycles,
            "proxy": proxy,
        }
        candidates.append((plan, info))
    if not candidates:
        return {}
    rankers = {
        "mapping_locality_one_move": lambda item: (
            item[1]["proxy"]["estimated_makespan_cycles"]
            - item[1]["estimated_l2_read_cycles_saved"],
            item[1]["proxy"]["max_memory_excess_bytes"],
            item[1]["proxy"]["cross_payload_bytes"],
            item[1]["moved_subgraph"], item[1]["destination_core"],
        ),
        "mapping_balanced_one_move": lambda item: (
            item[1]["proxy"]["estimated_makespan_cycles"]
            - item[1]["estimated_l2_read_cycles_saved"] // 2,
            item[1]["proxy"]["pipe_bound_cycles"],
            item[1]["proxy"]["max_memory_excess_bytes"],
            item[1]["moved_subgraph"], item[1]["destination_core"],
        ),
    }
    return {
        name: min(candidates, key=key)
        for name, key in rankers.items()
    }
