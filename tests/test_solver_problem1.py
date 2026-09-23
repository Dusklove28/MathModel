"""Unit tests for GraphInfo and the deterministic B0 plan."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from solver_problem1 import GraphInfo, solve_problem1


class GraphInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(
            (PROJECT_DIR / "data" / "case_001.json").read_text(encoding="utf-8"))

    def test_topological_order_covers_every_eligible_op_once(self) -> None:
        info = GraphInfo.from_graph(self.graph)
        self.assertEqual(len(info.topo_order), len(info.eligible_ops))
        self.assertEqual(set(info.topo_order), set(info.eligible_ops))
        self.assertEqual(len(info.topo_order), len(set(info.topo_order)))

    def test_topological_order_satisfies_every_contracted_dependency(self) -> None:
        info = GraphInfo.from_graph(self.graph)
        position = {op_id: index for index, op_id in enumerate(info.topo_order)}
        for source in info.eligible_ops:
            for target in info.succs[source]:
                self.assertLess(position[source], position[target])

    def test_solver_is_deterministic_and_has_exact_output_fields(self) -> None:
        first = solve_problem1(self.graph, 4)
        second = solve_problem1(self.graph, 4)
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"node_to_subgraph", "core_schedules"})


if __name__ == "__main__":
    unittest.main()
