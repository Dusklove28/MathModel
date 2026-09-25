"""Small official-evaluator tests for the bounded Problem-3 ordering chain."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "code"))

from candidate_manager_problem3 import run_candidate_group
from multicore_cut_evaluate_problem_3 import evaluate_problem_3
from run_problem3_g1 import json_sha256, write_json
from solver_problem2_stage3 import subgraph_core_assignment
from solver_problem3 import generate_reuse_window_candidates
from solver_problem3_mapping import generate_shared_input_moves
from stub_multicore_cut_and_schedule import derive_multicore_plan


SETTINGS = {
    "bandwidth": 60, "capacity": {"L1": 524288, "UB": 131072},
    "cross_core_copy_delay_cycles": 500,
    "cache_capacity_bytes": 1048576,
    "cache_bandwidth_bytes_per_cycle": 250,
}


def example():
    graph = {
        "ops": [
            {"id": 100 + i, "op": "ADD", "pipe": "PIPE_V", "cycles": 50}
            for i in range(3)
        ],
        "tensors": [
            {"id": 10, "pos": "DDR", "size": 64},
            {"id": 20, "pos": "DDR", "size": 128},
        ],
        "edges": [
            {"source": 10, "target": 100},
            {"source": 20, "target": 101},
            {"source": 10, "target": 102},
        ],
    }
    plan = {
        "node_to_subgraph": {"100": 0, "101": 1, "102": 2},
        "core_schedules": [[0, 1], [2]],
    }
    return graph, plan


class Problem3OrderingTests(unittest.TestCase):
    def test_two_deterministic_legal_fixed_mapping_candidates(self):
        graph, plan = example()
        parameters = {
            "bandwidth": SETTINGS["bandwidth"],
            "capacity": SETTINGS["capacity"],
            "cross_core_delay": SETTINGS["cross_core_copy_delay_cycles"],
            "cache_capacity_bytes": SETTINGS["cache_capacity_bytes"],
            "cache_bandwidth_bytes_per_cycle":
                SETTINGS["cache_bandwidth_bytes_per_cycle"],
        }
        first = generate_reuse_window_candidates(graph, plan, **parameters)
        second = generate_reuse_window_candidates(graph, plan, **parameters)
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"reuse_window", "balanced_window"})
        assignment = subgraph_core_assignment(plan)
        for candidate, diagnostics in first.values():
            self.assertEqual(subgraph_core_assignment(candidate), assignment)
            self.assertEqual(candidate["node_to_subgraph"], plan["node_to_subgraph"])
            derive_multicore_plan(graph, candidate)
            result = evaluate_problem_3(
                graph, candidate, SETTINGS["bandwidth"], SETTINGS["capacity"],
                SETTINGS["cross_core_copy_delay_cycles"],
                SETTINGS["cache_capacity_bytes"],
                SETTINGS["cache_bandwidth_bytes_per_cycle"],
            )
            self.assertEqual(result["num_cores"], 2)
            self.assertTrue(diagnostics["fixed_mapping"])

    def test_baseline_fallback_and_verified_resume(self):
        graph, plan = example()
        baseline = evaluate_problem_3(
            graph, plan, SETTINGS["bandwidth"], SETTINGS["capacity"],
            SETTINGS["cross_core_copy_delay_cycles"],
            SETTINGS["cache_capacity_bytes"],
            SETTINGS["cache_bandwidth_bytes_per_cycle"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path = root / "g1_plan.json"
            l2_path = root / "g1_l2.json"
            g1_manifest_path = root / "g1_group.json"
            write_json(plan_path, plan)
            write_json(l2_path, baseline)
            write_json(g1_manifest_path, {"case": "case_001", "cores": 2})
            kwargs = {
                "graph": graph, "graph_hash": json_sha256(graph),
                "baseline_plan": plan, "baseline_l2": baseline,
                "baseline_plan_path": plan_path,
                "baseline_l2_path": l2_path,
                "baseline_g1_manifest_path": g1_manifest_path,
                "case": "case_001", "cores": 2,
                "settings": SETTINGS, "config_lf_hash": "test-config",
                "official_hashes": {"test-evaluator": "abc"},
                "implementation_hashes": {"test-implementation": "def"},
                "group_dir": root / "out", "cache_dir": root / "cache",
            }
            first = run_candidate_group(**kwargs)
            self.assertEqual(first["status"], "success")
            self.assertLessEqual(first["winner_makespan"], baseline["makespan"])
            self.assertIn("baseline", [item["name"] for item in first["candidates"]])
            with patch("candidate_manager_problem3.evaluate_problem_3",
                       side_effect=AssertionError("unexpected official call")):
                resumed = run_candidate_group(**kwargs)
            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["official_calls_this_invocation"], 0)

    def test_mapping_proposals_move_one_subgraph_and_pass_official_evaluator(self):
        graph, plan = example()
        arguments = {
            "bandwidth": SETTINGS["bandwidth"],
            "capacity": SETTINGS["capacity"],
            "cross_core_delay": SETTINGS["cross_core_copy_delay_cycles"],
            "cache_bandwidth": SETTINGS["cache_bandwidth_bytes_per_cycle"],
        }
        first = generate_shared_input_moves(graph, plan, **arguments)
        self.assertEqual(first, generate_shared_input_moves(graph, plan, **arguments))
        self.assertGreaterEqual(len(first), 1)
        self.assertLessEqual(len(first), 2)
        original = subgraph_core_assignment(plan)
        for candidate, diagnostics in first.values():
            self.assertEqual(candidate["node_to_subgraph"], plan["node_to_subgraph"])
            moved = sum(core != original[sg] for sg, core in
                        subgraph_core_assignment(candidate).items())
            self.assertEqual(moved, 1)
            derive_multicore_plan(graph, candidate)
            result = evaluate_problem_3(
                graph, candidate, SETTINGS["bandwidth"], SETTINGS["capacity"],
                SETTINGS["cross_core_copy_delay_cycles"],
                SETTINGS["cache_capacity_bytes"],
                SETTINGS["cache_bandwidth_bytes_per_cycle"],
            )
            self.assertEqual(result["num_cores"], 2)
            self.assertGreater(diagnostics["shared_input_read_bytes_saved"], 0)


if __name__ == "__main__":
    unittest.main()
