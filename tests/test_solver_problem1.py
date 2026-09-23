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
    build_b0_subgraphs,
    build_b1_subgraphs,
    solve_problem1,
    solve_problem1_with_diagnostics,
)


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


if __name__ == "__main__":
    unittest.main()
