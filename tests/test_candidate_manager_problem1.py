import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import candidate_manager_problem1 as manager
from candidate_manager_problem1 import (
    CandidatePlan,
    EvaluationCache,
    canonical_plan_signature,
    run_candidate_manager,
    select_best_candidate,
)
from solver_problem1 import build_single_candidate
from stub_multicore_cut_and_schedule import derive_multicore_plan


def _small_graph():
    return {
        "ops": [
            {"id": 1, "op": "ADD", "pipe": "PIPE_V", "cycles": 10},
            {"id": 2, "op": "ADD", "pipe": "PIPE_V", "cycles": 10},
        ],
        "tensors": [
            {"id": 100, "pos": "UB", "size": 32},
        ],
        "edges": [
            {"source": 1, "target": 100},
            {"source": 100, "target": 2},
        ],
    }


def _mock_result(plan, makespan=None, added=0):
    subgraphs = len(set(plan["node_to_subgraph"].values()))
    return {
        "makespan": subgraphs if makespan is None else makespan,
        "data_movement_bytes": {"added_copy_bytes": added},
        "memory_peak_by_core": {"0": {"L1": 10, "UB": 20}},
    }


class CandidateManagerTests(unittest.TestCase):
    def test_single_candidate_is_officially_valid(self):
        graph = _small_graph()
        plan = build_single_candidate(graph, 4)
        self.assertEqual(plan["node_to_subgraph"], {1: 0, 2: 0})
        self.assertEqual(plan["core_schedules"], [[0], [], [], []])
        derive_multicore_plan(graph, plan)

    def test_canonicalization_ignores_only_subgraph_ids(self):
        left = {
            "node_to_subgraph": {1: 0, 2: 0, 3: 1},
            "core_schedules": [[0, 1], []],
        }
        right = {
            "node_to_subgraph": {1: 8, 2: 8, 3: 3},
            "core_schedules": [[8, 3], []],
        }
        self.assertEqual(
            canonical_plan_signature(left), canonical_plan_signature(right))

    def test_canonicalization_keeps_core_identity(self):
        left = {
            "node_to_subgraph": {1: 0, 2: 1},
            "core_schedules": [[0], [1]],
        }
        right = {
            "node_to_subgraph": {1: 7, 2: 9},
            "core_schedules": [[9], [7]],
        }
        self.assertNotEqual(
            canonical_plan_signature(left), canonical_plan_signature(right))

    def test_dedup_evaluates_each_unique_plan_once_and_cache_hits(self):
        calls = []

        def evaluator(graph, plan, config):
            calls.append(canonical_plan_signature(plan))
            return _mock_result(plan)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_path = root / "case_test.json"
            config_path = root / "config.txt"
            graph_path.write_text(json.dumps(_small_graph()), encoding="utf-8")
            config_path.write_text("test config", encoding="utf-8")
            first = run_candidate_manager(
                graph_path, 4, config_path=config_path,
                output_root=root / "out", cache_dir=root / "cache",
                evaluator=evaluator)
            self.assertEqual(
                len(calls), first.manifest["unique_plans"])
            self.assertLess(first.manifest["unique_plans"], 6)
            self.assertEqual(first.manifest["official_evaluations"], len(calls))
            self.assertEqual(first.manifest["cache_hits"], 0)

            calls.clear()
            second = run_candidate_manager(
                graph_path, 4, config_path=config_path,
                output_root=root / "out", cache_dir=root / "cache",
                evaluator=evaluator)
            self.assertEqual(calls, [])
            self.assertEqual(
                second.manifest["cache_hits"], second.manifest["unique_plans"])
            self.assertEqual(first.final_plan, second.final_plan)

    def test_lexicographic_selection(self):
        a = CandidatePlan("single", "SINGLE", {}, status="evaluated",
                          makespan=1000, added_copy_bytes=500)
        b = CandidatePlan("b0", "B0", {}, status="evaluated",
                          makespan=1001, added_copy_bytes=0)
        self.assertIs(select_best_candidate([a, b]), a)
        b.makespan = 1000
        b.added_copy_bytes = 400
        self.assertIs(select_best_candidate([a, b]), b)

    def test_generation_failure_is_isolated(self):
        calls = []
        original = manager.generate_problem1_plan_from_graph_info

        def flaky(graph, cores, method="B0", windows=None):
            if method == "B2A" and windows == 16:
                raise RuntimeError("synthetic B2A failure")
            return original(graph, cores, method=method, windows=windows)

        def evaluator(graph, plan, config):
            calls.append(1)
            return _mock_result(plan, makespan=100, added=10)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_path = root / "case_test.json"
            config_path = root / "config.txt"
            graph_path.write_text(json.dumps(_small_graph()), encoding="utf-8")
            config_path.write_text("test config", encoding="utf-8")
            with patch.object(
                    manager, "generate_problem1_plan_from_graph_info",
                    side_effect=flaky):
                result = run_candidate_manager(
                    graph_path, 2, config_path=config_path,
                    output_root=root / "out", cache_dir=root / "cache",
                    evaluator=evaluator)
            failed = {candidate["name"]: candidate
                      for candidate in result.manifest["candidates"]}
            self.assertEqual(failed["b2a_w16"]["status"], "failed")
            self.assertIn("synthetic B2A failure", failed["b2a_w16"]["error"])
            self.assertIn(result.winner_name, {"single", "b0", "b1", "b2a_w4", "b2a_w8"})
            self.assertTrue(calls)

    def test_cache_rejects_any_stale_hash(self):
        result = _mock_result({
            "node_to_subgraph": {1: 0}, "core_schedules": [[0]]},
            makespan=12, added=34)
        with tempfile.TemporaryDirectory() as temporary:
            cache = EvaluationCache(Path(temporary))
            cache.store(
                graph_hash="graph-a", config_hash="config-a",
                plan_hash="plan-a", evaluator_hash="evaluator-a",
                result=result, evaluation_time=0.1, max_memory_bytes=20,
                metadata={})
            self.assertIsNotNone(cache.lookup(
                "graph-a", "config-a", "plan-a", "evaluator-a"))
            self.assertIsNone(cache.lookup(
                "graph-b", "config-a", "plan-a", "evaluator-a"))
            self.assertIsNone(cache.lookup(
                "graph-a", "config-b", "plan-a", "evaluator-a"))
            self.assertIsNone(cache.lookup(
                "graph-a", "config-a", "plan-a", "evaluator-b"))

    def test_cache_remains_valid_after_directory_is_moved(self):
        result = _mock_result({
            "node_to_subgraph": {1: 0}, "core_schedules": [[0]]},
            makespan=12, added=34)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            moved = root / "moved"
            cache = EvaluationCache(original)
            cache.store(
                graph_hash="graph-a", config_hash="config-a",
                plan_hash="plan-a", evaluator_hash="evaluator-a",
                result=result, evaluation_time=0.1, max_memory_bytes=20,
                metadata={})
            shutil.copytree(original, moved)
            shutil.rmtree(original)
            entry = EvaluationCache(moved).lookup(
                "graph-a", "config-a", "plan-a", "evaluator-a")
            self.assertIsNotNone(entry)
            self.assertTrue(Path(entry["full_result_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
