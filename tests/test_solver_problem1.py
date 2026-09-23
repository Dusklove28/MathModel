"""Unit tests for GraphInfo and the deterministic B0/B1 plans."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from solver_problem1 import (
    OFFICIAL_CODE_DIR,
    GraphInfo,
    _build_branch_component,
    build_b0_subgraphs,
    build_b1_subgraphs,
    build_b2a_subgraphs,
    build_level_windows,
    solve_problem1,
    solve_problem1_with_diagnostics,
)
from multicore_cut_evaluate_problem_1 import evaluate_scene_a


def _make_tensor_graph(op_ids, tensor_specs):
    """Build a small valid graph from (producer, consumers, size) tensors."""

    ops = [
        {"id": op_id, "op": "ADD", "pipe": "PIPE_V", "cycles": 10}
        for op_id in sorted(op_ids)
    ]
    tensors = []
    edges = []
    for offset, (producer, consumers, size) in enumerate(tensor_specs):
        tensor_id = 10000 + offset
        tensors.append({"id": tensor_id, "pos": "UB", "size": size})
        if producer is not None:
            edges.append({"source": producer, "target": tensor_id})
        for consumer in consumers:
            edges.append({"source": tensor_id, "target": consumer})
    return {"ops": ops, "tensors": tensors, "edges": edges}


def _chain_graph(chains):
    specs = []
    op_ids = {op_id for chain in chains for op_id in chain}
    for chain in chains:
        specs.append((None, [chain[0]], 32))
        specs.extend((source, [target], 32)
                     for source, target in zip(chain, chain[1:]))
        specs.append((chain[-1], [], 32))
    return _make_tensor_graph(op_ids, specs)


class GraphInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(
            (PROJECT_DIR / "data" / "case_001.json").read_text(encoding="utf-8"))
        cls.info = GraphInfo.from_graph(cls.graph)

    def test_topological_order_covers_every_eligible_op_once(self) -> None:
        info = self.info
        self.assertEqual(len(info.topo_order), len(info.eligible_ops))
        self.assertEqual(set(info.topo_order), set(info.eligible_ops))
        self.assertEqual(len(info.topo_order), len(set(info.topo_order)))

    def test_topological_order_satisfies_every_contracted_dependency(self) -> None:
        info = self.info
        position = {op_id: index for index, op_id in enumerate(info.topo_order)}
        for source in info.eligible_ops:
            for target in info.succs[source]:
                self.assertLess(position[source], position[target])

    def test_solver_is_deterministic_and_has_exact_output_fields(self) -> None:
        first = solve_problem1(self.graph, 4)
        second = solve_problem1(self.graph, 4)
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"node_to_subgraph", "core_schedules"})

    def test_subgraph_workload_is_maximum_of_two_compute_pipes(self) -> None:
        subgraphs = build_b0_subgraphs(self.info, 4)
        for subgraph_id in subgraphs.nodes:
            self.assertEqual(
                subgraphs.workload[subgraph_id],
                max(
                    subgraphs.pipe_m_cycles[subgraph_id],
                    subgraphs.pipe_v_cycles[subgraph_id],
                ),
            )

    def test_b1_is_deterministic_topological_and_keeps_4k_blocks(self) -> None:
        first = build_b1_subgraphs(self.info, 4)
        second = build_b1_subgraphs(self.info, 4)
        self.assertEqual(first.node_to_subgraph, second.node_to_subgraph)
        self.assertEqual(len(first.nodes), 16)
        position = {
            op_id: index
            for index, op_id in enumerate(
                node for subgraph_id in sorted(first.nodes)
                for node in first.nodes[subgraph_id]
            )
        }
        for source in self.info.eligible_ops:
            for target in self.info.succs[source]:
                self.assertLess(position[source], position[target])

    def test_diagnostics_do_not_change_official_plan_fields(self) -> None:
        plan, diagnostics = solve_problem1_with_diagnostics(
            self.graph, 4, method="B1")
        self.assertEqual(set(plan), {"node_to_subgraph", "core_schedules"})
        self.assertEqual(diagnostics["num_subgraphs"], 16)
        self.assertIn("longest_path_ratio", diagnostics)
        self.assertIn("mean_ready_size", diagnostics)

    def test_official_code_directory_is_resolved_without_duplication(self) -> None:
        self.assertTrue((OFFICIAL_CODE_DIR / "evaluation_validation.py").is_file())
        self.assertTrue((OFFICIAL_CODE_DIR / "contest_io.py").is_file())


class B2ATests(unittest.TestCase):
    def test_interleaved_independent_chains_preserve_branches(self) -> None:
        graph = _chain_graph([
            (1, 5, 9),
            (2, 6, 10),
            (3, 7, 11),
            (4, 8, 12),
        ])
        info = GraphInfo.from_graph(graph)
        self.assertEqual(
            [info.level[node_id] for node_id in (1, 2, 3, 4)], [0, 0, 0, 0])
        self.assertEqual(
            [info.level[node_id] for node_id in (5, 6, 7, 8)], [1, 1, 1, 1])
        self.assertEqual(
            [info.level[node_id] for node_id in (9, 10, 11, 12)], [2, 2, 2, 2])
        for source in info.eligible_ops:
            for target in info.succs[source]:
                self.assertLess(info.level[source], info.level[target])

        result = build_b2a_subgraphs(info, num_cores=2, num_windows=4)
        self.assertEqual(result.actual_windows, 3)
        self.assertEqual(result.components_per_window, (4, 4, 4))
        self.assertEqual(result.components_after_packing, 12)
        plan, diagnostics = solve_problem1_with_diagnostics(
            graph, 2, method="B2A", windows=4)
        self.assertEqual(diagnostics["longest_path_nodes"], 3)
        self.assertGreaterEqual(diagnostics["max_ready_size"], 4)
        self.assertEqual(set(plan), {"node_to_subgraph", "core_schedules"})

    def test_serial_chain_does_not_invent_parallelism_and_evaluates(self) -> None:
        graph = _chain_graph([(1, 2, 3, 4)])
        plan, diagnostics = solve_problem1_with_diagnostics(
            graph, 4, method="B2A", windows=4)
        self.assertEqual(diagnostics["longest_path_ratio"], 1.0)
        self.assertEqual(diagnostics["max_ready_size"], 1)
        self.assertEqual(diagnostics["core_distribution"], [4, 0, 0, 0])
        result = evaluate_scene_a(
            graph,
            plan,
            bandwidth=60,
            capacity={"L1": 524288, "UB": 131072},
            cross_core_wait=1000,
            same_core_wait=100,
        )
        self.assertGreater(result["makespan"], 0)

    def test_fork_join_keeps_middle_branches_separate(self) -> None:
        graph = _make_tensor_graph(
            {1, 2, 3, 4},
            [
                (None, [1], 32),
                (1, [2], 32),
                (1, [3], 32),
                (2, [4], 32),
                (3, [4], 32),
                (4, [], 32),
            ],
        )
        info = GraphInfo.from_graph(graph)
        windows = build_level_windows(info, 4)
        self.assertEqual([window.levels for window in windows], [(0,), (1,), (2,)])
        result = build_b2a_subgraphs(info, num_cores=2, num_windows=4)
        self.assertEqual(result.components_per_window, (1, 2, 1))
        mapping = result.subgraphs.node_to_subgraph
        self.assertNotEqual(mapping[2], mapping[3])
        self.assertIn(mapping[2], result.subgraphs.preds[mapping[4]])
        self.assertIn(mapping[3], result.subgraphs.preds[mapping[4]])

    def test_shared_boundary_tensor_bytes_are_deduplicated(self) -> None:
        shared_size = 256
        graph = _make_tensor_graph(
            {1, 2, 3},
            [
                (None, [1], 32),
                (1, [2, 3], shared_size),
                (2, [3], 64),
                (3, [], 32),
            ],
        )
        info = GraphInfo.from_graph(graph)
        component = _build_branch_component(info, (2, 3))
        self.assertEqual(component.boundary_pred_tensors, (10001,))
        self.assertEqual(
            sum(info.tensor_size[tid]
                for tid in component.boundary_pred_tensors),
            shared_size,
        )


if __name__ == "__main__":
    unittest.main()
