import copy
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from evaluation_validation import validate_task_order
from solver_problem2_stage3 import (
    ORDER_POLICIES,
    generate_scene_b_order_candidates,
    subgraph_core_assignment,
)
from stub_multicore_cut_and_schedule import derive_multicore_plan


def _branch_graph():
    return {
        "ops": [
            {"id": 1, "op": "ADD", "pipe": "PIPE_V", "cycles": 20},
            {"id": 2, "op": "ADD", "pipe": "PIPE_V", "cycles": 5},
            {"id": 3, "op": "MUL", "pipe": "PIPE_M", "cycles": 20},
        ],
        "tensors": [
            {"id": 100, "pos": "UB", "size": 1024},
            {"id": 101, "pos": "UB", "size": 16},
        ],
        "edges": [
            {"source": 1, "target": 100},
            {"source": 100, "target": 3},
            {"source": 2, "target": 101},
        ],
    }


def _fixed_plan():
    return {
        "node_to_subgraph": {1: 0, 2: 1, 3: 2},
        "core_schedules": [[0, 1], [2]],
    }


class Problem2Stage3OrderingSolverTests(unittest.TestCase):
    def test_candidates_are_deterministic_legal_and_keep_mapping(self):
        graph = _branch_graph()
        fixed = _fixed_plan()
        before = copy.deepcopy(fixed)
        first = generate_scene_b_order_candidates(graph, fixed)
        second = generate_scene_b_order_candidates(graph, fixed)
        self.assertEqual(first, second)
        self.assertEqual(tuple(first), ORDER_POLICIES)
        self.assertEqual(fixed, before)
        fixed_assignment = subgraph_core_assignment(fixed)
        for plan, diagnostics in first.values():
            self.assertEqual(plan["node_to_subgraph"], fixed["node_to_subgraph"])
            self.assertEqual(subgraph_core_assignment(plan), fixed_assignment)
            validate_task_order(derive_multicore_plan(graph, plan))
            self.assertTrue(diagnostics["fixed_partition"])
            self.assertTrue(diagnostics["fixed_mapping"])
            self.assertIn("selection_trace", diagnostics)
        self.assertNotEqual(
            first["release"][0]["core_schedules"], fixed["core_schedules"])

    def test_policy_budget_and_assignment_validation(self):
        with self.assertRaisesRegex(Exception, "non-empty and unique"):
            generate_scene_b_order_candidates(
                _branch_graph(), _fixed_plan(),
                policies=("release", "release"))
        with self.assertRaisesRegex(Exception, "multiple cores"):
            subgraph_core_assignment({
                "node_to_subgraph": {},
                "core_schedules": [[0], [0]],
            })


if __name__ == "__main__":
    unittest.main()

