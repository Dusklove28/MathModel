"""Deterministic B0 solver for problem 1.

The submitted plan contains exactly the two fields required by the official
evaluator.  Graph validation, op-DAG construction, COPY-node contraction and
final plan validation are deliberately delegated to the official code.
"""

from __future__ import annotations

import heapq
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple


OFFICIAL_CODE_DIR = Path(__file__).resolve().parent / "code"
if str(OFFICIAL_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(OFFICIAL_CODE_DIR))

from evaluation_validation import require_integer, validate_graph  # noqa: E402
from stub_multicore_cut_and_schedule import (  # noqa: E402
    EXCLUDED_COPY_TYPES,
    _build_op_adjacency,
    _contract_excluded_copy_nodes,
    derive_multicore_plan,
)


PIPE_M = "PIPE_M"
PIPE_V = "PIPE_V"
TASK_SAME_CORE_WAIT = 100
TASK_CROSS_CORE_WAIT = 1000
COMMUNICATION_BYTES_PER_CYCLE = 60.0


class Problem1SolverError(RuntimeError):
    """The validated graph cannot be transformed into a B0 plan."""


Dependency = Tuple[int, int]


@dataclass(frozen=True)
class GraphInfo:
    """Read-only-by-convention feature view of the eligible op DAG.

    ``tensor_producer`` stores a tuple because the official graph validator
    permits more than one producer, even though official cases normally have
    one. ``dependency_tensors[(u, v)]`` contains the tensor ids encountered on
    the contracted dependency from eligible op ``u`` to eligible op ``v``.
    """

    eligible_ops: Tuple[int, ...]
    topo_order: Tuple[int, ...]
    preds: Mapping[int, Tuple[int, ...]]
    succs: Mapping[int, Tuple[int, ...]]
    op_pipe: Mapping[int, str]
    op_cycles: Mapping[int, int]
    pipe_m_cycles: Mapping[int, int]
    pipe_v_cycles: Mapping[int, int]
    op_workload: Mapping[int, int]
    b_level: Mapping[int, int]
    tensor_size: Mapping[int, int]
    tensor_pos: Mapping[int, str]
    tensor_producer: Mapping[int, Tuple[int, ...]]
    tensor_consumers: Mapping[int, Tuple[int, ...]]
    op_input_tensors: Mapping[int, Tuple[int, ...]]
    op_output_tensors: Mapping[int, Tuple[int, ...]]
    op_input_bytes: Mapping[int, int]
    op_output_bytes: Mapping[int, int]
    dependency_tensors: Mapping[Dependency, Tuple[int, ...]]
    dependency_bytes: Mapping[Dependency, int]
    dependency_direct_bytes: Mapping[Dependency, int]

    @classmethod
    def from_graph(cls, graph_json: Mapping[str, Any]) -> "GraphInfo":
        """Validate an Op-Tensor graph and build all phase-A/B features."""

        validate_graph(graph_json)
        op_by_id = {op["id"]: op for op in graph_json["ops"]}
        tensor_by_id = {tensor["id"]: tensor for tensor in graph_json["tensors"]}
        op_ids = set(op_by_id)

        eligible_ops = tuple(sorted(
            op_id for op_id, op in op_by_id.items()
            if op.get("op") not in EXCLUDED_COPY_TYPES
        ))
        eligible_set = set(eligible_ops)

        # Reuse the official implementation for all legality-sensitive graph
        # construction and COPY_IN/COPY_OUT contraction.
        _, full_succs = _build_op_adjacency(graph_json)
        contracted_preds, contracted_succs = _contract_excluded_copy_nodes(
            eligible_ops, full_succs)

        tensor_producers: Dict[int, Set[int]] = {
            tid: set() for tid in tensor_by_id
        }
        tensor_consumers: Dict[int, Set[int]] = {
            tid: set() for tid in tensor_by_id
        }
        op_inputs: Dict[int, Set[int]] = {op_id: set() for op_id in eligible_ops}
        op_outputs: Dict[int, Set[int]] = {op_id: set() for op_id in eligible_ops}
        full_arc_tensors: Dict[Dependency, Set[int]] = defaultdict(set)
        direct_bytes: Dict[Dependency, int] = defaultdict(int)

        for edge in graph_json["edges"]:
            src, dst = edge["source"], edge["target"]
            src_is_op, dst_is_op = src in op_ids, dst in op_ids
            if src_is_op and not dst_is_op:
                tensor_producers[dst].add(src)
                if src in eligible_set:
                    op_outputs[src].add(dst)
            elif not src_is_op and dst_is_op:
                tensor_consumers[src].add(dst)
                if dst in eligible_set:
                    op_inputs[dst].add(src)
            elif src_is_op and dst_is_op and src != dst:
                value = edge.get("data_size", 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    direct_bytes[(src, dst)] += max(0, int(value))

        for tid, producers in tensor_producers.items():
            for src in producers:
                for dst in tensor_consumers.get(tid, ()):
                    if src != dst:
                        full_arc_tensors[(src, dst)].add(tid)

        dependency_tensors = _contracted_dependency_tensors(
            eligible_ops=eligible_ops,
            eligible_set=eligible_set,
            full_succs=full_succs,
            full_arc_tensors=full_arc_tensors,
            contracted_succs=contracted_succs,
        )
        contracted_direct_bytes = {
            pair: direct_bytes.get(pair, 0)
            for pair in dependency_tensors
        }
        tensor_size = {
            tid: tensor["size"] for tid, tensor in tensor_by_id.items()
        }
        dependency_bytes = {
            pair: (
                sum(tensor_size[tid] for tid in tids)
                + contracted_direct_bytes[pair]
            )
            for pair, tids in dependency_tensors.items()
        }

        preds = {
            op_id: tuple(sorted(contracted_preds[op_id]))
            for op_id in eligible_ops
        }
        succs = {
            op_id: tuple(sorted(contracted_succs[op_id]))
            for op_id in eligible_ops
        }
        topo_order = tuple(_deterministic_topological_order(
            eligible_ops, preds, succs))

        op_pipe = {op_id: op_by_id[op_id]["pipe"] for op_id in eligible_ops}
        op_cycles = {op_id: op_by_id[op_id]["cycles"] for op_id in eligible_ops}
        pipe_m_cycles = {
            op_id: op_cycles[op_id] if op_pipe[op_id] == PIPE_M else 0
            for op_id in eligible_ops
        }
        pipe_v_cycles = {
            op_id: op_cycles[op_id] if op_pipe[op_id] == PIPE_V else 0
            for op_id in eligible_ops
        }
        op_workload = {
            op_id: pipe_m_cycles[op_id] + pipe_v_cycles[op_id]
            for op_id in eligible_ops
        }
        b_level = _reverse_critical_path(topo_order, succs, op_workload)

        input_tensors = {
            op_id: tuple(sorted(op_inputs[op_id])) for op_id in eligible_ops
        }
        output_tensors = {
            op_id: tuple(sorted(op_outputs[op_id])) for op_id in eligible_ops
        }

        return cls(
            eligible_ops=eligible_ops,
            topo_order=topo_order,
            preds=preds,
            succs=succs,
            op_pipe=op_pipe,
            op_cycles=op_cycles,
            pipe_m_cycles=pipe_m_cycles,
            pipe_v_cycles=pipe_v_cycles,
            op_workload=op_workload,
            b_level=b_level,
            tensor_size=tensor_size,
            tensor_pos={
                tid: tensor["pos"] for tid, tensor in tensor_by_id.items()
            },
            tensor_producer={
                tid: tuple(sorted(producers))
                for tid, producers in tensor_producers.items()
            },
            tensor_consumers={
                tid: tuple(sorted(consumers))
                for tid, consumers in tensor_consumers.items()
            },
            op_input_tensors=input_tensors,
            op_output_tensors=output_tensors,
            op_input_bytes={
                op_id: sum(tensor_size[tid] for tid in input_tensors[op_id])
                for op_id in eligible_ops
            },
            op_output_bytes={
                op_id: sum(tensor_size[tid] for tid in output_tensors[op_id])
                for op_id in eligible_ops
            },
            dependency_tensors={
                pair: tuple(sorted(tids))
                for pair, tids in dependency_tensors.items()
            },
            dependency_bytes=dependency_bytes,
            dependency_direct_bytes=contracted_direct_bytes,
        )


def _contracted_dependency_tensors(
    eligible_ops: Sequence[int],
    eligible_set: Set[int],
    full_succs: Mapping[int, Set[int]],
    full_arc_tensors: Mapping[Dependency, Set[int]],
    contracted_succs: Mapping[int, Set[int]],
) -> Dict[Dependency, Set[int]]:
    """Attach original tensor ids to the official contracted op dependencies."""

    result: Dict[Dependency, Set[int]] = {
        (src, dst): set()
        for src in eligible_ops for dst in contracted_succs[src]
    }
    for source in eligible_ops:
        payload_at_excluded: Dict[int, Set[int]] = {}
        queued: Set[int] = set()
        queue: deque[int] = deque()

        def visit(node: int, payload: Iterable[int]) -> None:
            if node in eligible_set:
                if node != source and (source, node) in result:
                    result[(source, node)].update(payload)
                return
            current = payload_at_excluded.setdefault(node, set())
            old_size = len(current)
            current.update(payload)
            if node not in queued or len(current) != old_size:
                queue.append(node)
                queued.add(node)

        for nxt in sorted(full_succs[source]):
            visit(nxt, full_arc_tensors.get((source, nxt), ()))

        while queue:
            node = queue.popleft()
            queued.discard(node)
            base_payload = payload_at_excluded[node]
            for nxt in sorted(full_succs.get(node, ())):
                visit(
                    nxt,
                    set(base_payload) | full_arc_tensors.get((node, nxt), set()),
                )
    return result


def _deterministic_topological_order(
    node_ids: Sequence[int],
    preds: Mapping[int, Sequence[int]],
    succs: Mapping[int, Sequence[int]],
) -> List[int]:
    """Kahn order with smallest-op-id tie breaking."""

    indegree = {node_id: len(preds[node_id]) for node_id in node_ids}
    ready = [node_id for node_id in node_ids if indegree[node_id] == 0]
    heapq.heapify(ready)
    order: List[int] = []
    while ready:
        node_id = heapq.heappop(ready)
        order.append(node_id)
        for successor in succs[node_id]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(ready, successor)
    if len(order) != len(node_ids):
        unresolved = sorted(set(node_ids) - set(order))
        raise Problem1SolverError(
            "contracted eligible op graph contains a cycle; unresolved={}".format(
                unresolved[:20]))
    return order


def _reverse_critical_path(
    topo_order: Sequence[int],
    succs: Mapping[int, Sequence[int]],
    workload: Mapping[int, int],
) -> Dict[int, int]:
    """Compute b-level = own M/V cycles + longest successor b-level."""

    rank: Dict[int, int] = {}
    for node_id in reversed(topo_order):
        tail = max((rank[succ] for succ in succs[node_id]), default=0)
        rank[node_id] = workload[node_id] + tail
    return rank


@dataclass(frozen=True)
class SubgraphInfo:
    node_to_subgraph: Mapping[int, int]
    nodes: Mapping[int, Tuple[int, ...]]
    preds: Mapping[int, Tuple[int, ...]]
    succs: Mapping[int, Tuple[int, ...]]
    workload: Mapping[int, int]
    edge_tensor_ids: Mapping[Dependency, Tuple[int, ...]]
    edge_bytes: Mapping[Dependency, int]
    rank: Mapping[int, float]


def _partition_by_cumulative_work(
    topo_order: Sequence[int],
    workload: Mapping[int, int],
    requested_parts: int,
) -> List[Tuple[int, ...]]:
    """Create exactly min(requested_parts, n) non-empty contiguous ranges."""

    if not topo_order:
        return []
    parts = min(requested_parts, len(topo_order))
    remaining_work = sum(workload[node_id] for node_id in topo_order)
    start = 0
    ranges: List[Tuple[int, ...]] = []

    for part_index in range(parts):
        remaining_parts = parts - part_index
        if remaining_parts == 1:
            end = len(topo_order)
            part_work = sum(workload[node_id] for node_id in topo_order[start:end])
        elif remaining_work == 0:
            # Equal-count fallback is deterministic when the remaining proxy
            # work is all zero.
            remaining_nodes = len(topo_order) - start
            take = max(1, remaining_nodes // remaining_parts)
            end = start + take
            part_work = 0
        else:
            target = remaining_work / remaining_parts
            max_end = len(topo_order) - (remaining_parts - 1)
            end = start
            part_work = 0
            while end < max_end:
                next_work = workload[topo_order[end]]
                if (end > start
                        and abs(part_work - target)
                        <= abs(part_work + next_work - target)):
                    break
                part_work += next_work
                end += 1
                if part_work >= target:
                    break
            if end == start:
                part_work = workload[topo_order[end]]
                end += 1

        ranges.append(tuple(topo_order[start:end]))
        remaining_work -= part_work
        start = end

    if start != len(topo_order) or any(not nodes for nodes in ranges):
        raise Problem1SolverError("internal error while partitioning topological order")
    return ranges


def build_b0_subgraphs(graph: GraphInfo, num_cores: int) -> SubgraphInfo:
    """Split the deterministic topo order into about 4*k work-balanced ranges."""

    require_integer(num_cores, "num_cores", 1)
    if num_cores <= 0:
        raise Problem1SolverError("num_cores must be positive")
    if not graph.topo_order:
        raise Problem1SolverError("problem 1 requires at least one eligible op")

    ranges = _partition_by_cumulative_work(
        graph.topo_order, graph.op_workload, requested_parts=4 * num_cores)
    nodes = {subgraph_id: node_ids
             for subgraph_id, node_ids in enumerate(ranges)}
    node_to_subgraph = {
        node_id: subgraph_id
        for subgraph_id, node_ids in nodes.items()
        for node_id in node_ids
    }
    subgraph_ids = tuple(nodes)
    pred_sets = {subgraph_id: set() for subgraph_id in subgraph_ids}
    succ_sets = {subgraph_id: set() for subgraph_id in subgraph_ids}
    edge_tensors: Dict[Dependency, Set[int]] = defaultdict(set)
    edge_direct_bytes: Dict[Dependency, int] = defaultdict(int)

    for src in graph.topo_order:
        src_subgraph = node_to_subgraph[src]
        for dst in graph.succs[src]:
            dst_subgraph = node_to_subgraph[dst]
            if src_subgraph == dst_subgraph:
                continue
            pair = (src_subgraph, dst_subgraph)
            succ_sets[src_subgraph].add(dst_subgraph)
            pred_sets[dst_subgraph].add(src_subgraph)
            edge_tensors[pair].update(graph.dependency_tensors[(src, dst)])
            edge_direct_bytes[pair] += graph.dependency_direct_bytes[(src, dst)]

    edge_bytes = {
        pair: (
            sum(graph.tensor_size[tid] for tid in tensor_ids)
            + edge_direct_bytes[pair]
        )
        for pair, tensor_ids in edge_tensors.items()
    }
    # Direct op-op dependencies may carry no tensor id, so make sure those
    # pairs are represented as well.
    for src in subgraph_ids:
        for dst in succ_sets[src]:
            pair = (src, dst)
            edge_tensors.setdefault(pair, set())
            edge_bytes.setdefault(pair, edge_direct_bytes[pair])

    subgraph_workload = {
        subgraph_id: sum(graph.op_workload[node_id] for node_id in node_ids)
        for subgraph_id, node_ids in nodes.items()
    }
    rank: Dict[int, float] = {}
    for subgraph_id in reversed(subgraph_ids):
        tail = max((
            edge_bytes[(subgraph_id, succ)] / COMMUNICATION_BYTES_PER_CYCLE
            + rank[succ]
            for succ in succ_sets[subgraph_id]
        ), default=0.0)
        rank[subgraph_id] = subgraph_workload[subgraph_id] + tail

    return SubgraphInfo(
        node_to_subgraph=node_to_subgraph,
        nodes=nodes,
        preds={sg: tuple(sorted(pred_sets[sg])) for sg in subgraph_ids},
        succs={sg: tuple(sorted(succ_sets[sg])) for sg in subgraph_ids},
        workload=subgraph_workload,
        edge_tensor_ids={
            pair: tuple(sorted(tids)) for pair, tids in edge_tensors.items()
        },
        edge_bytes=edge_bytes,
        rank=rank,
    )


def ready_list_eft_schedule(
    subgraphs: SubgraphInfo,
    num_cores: int,
) -> List[List[int]]:
    """Schedule ready subgraphs by descending b-level and minimum EFT."""

    subgraph_ids = tuple(sorted(subgraphs.nodes))
    indegree = {sg: len(subgraphs.preds[sg]) for sg in subgraph_ids}
    ready = [(-subgraphs.rank[sg], sg) for sg in subgraph_ids if indegree[sg] == 0]
    heapq.heapify(ready)

    core_schedules: List[List[int]] = [[] for _ in range(num_cores)]
    core_available = [0.0] * num_cores
    assigned_core: Dict[int, int] = {}
    finish_time: Dict[int, float] = {}
    scheduled_count = 0

    while ready:
        _, subgraph_id = heapq.heappop(ready)
        best: Tuple[float, int, float] | None = None
        for core_id in range(num_cores):
            core_release = core_available[core_id]
            if core_schedules[core_id]:
                core_release += TASK_SAME_CORE_WAIT

            dependency_release = 0.0
            for predecessor in subgraphs.preds[subgraph_id]:
                wait = (
                    TASK_SAME_CORE_WAIT
                    if assigned_core[predecessor] == core_id
                    else TASK_CROSS_CORE_WAIT
                )
                communication = (
                    subgraphs.edge_bytes[(predecessor, subgraph_id)]
                    / COMMUNICATION_BYTES_PER_CYCLE
                )
                dependency_release = max(
                    dependency_release,
                    finish_time[predecessor] + wait + communication,
                )

            estimated_start = max(core_release, dependency_release)
            eft = estimated_start + subgraphs.workload[subgraph_id]
            candidate = (eft, core_id, estimated_start)
            if best is None or candidate < best:
                best = candidate

        assert best is not None
        eft, core_id, _ = best
        # Append immediately: this order is part of the scheduling decision and
        # must not be re-sorted after assignment.
        core_schedules[core_id].append(subgraph_id)
        assigned_core[subgraph_id] = core_id
        finish_time[subgraph_id] = eft
        core_available[core_id] = eft
        scheduled_count += 1

        for successor in subgraphs.succs[subgraph_id]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(ready, (-subgraphs.rank[successor], successor))

    if scheduled_count != len(subgraph_ids):
        raise Problem1SolverError("subgraph DAG contains a cycle")
    return core_schedules


def solve_problem1(graph_json: Mapping[str, Any], num_cores: int) -> Dict[str, Any]:
    """Build and officially validate a deterministic B0 problem-1 plan."""

    graph = GraphInfo.from_graph(graph_json)
    subgraphs = build_b0_subgraphs(graph, num_cores)
    core_schedules = ready_list_eft_schedule(subgraphs, num_cores)
    plan = {
        "node_to_subgraph": dict(subgraphs.node_to_subgraph),
        "core_schedules": core_schedules,
    }
    # This is the same official validator used by the evaluator.  It checks
    # exact eligible-node coverage, unique subgraph scheduling, quotient-DAG
    # acyclicity and same-core dependency order.
    derive_multicore_plan(graph_json, plan)
    return plan


__all__ = [
    "GraphInfo",
    "Problem1SolverError",
    "SubgraphInfo",
    "build_b0_subgraphs",
    "ready_list_eft_schedule",
    "solve_problem1",
]
