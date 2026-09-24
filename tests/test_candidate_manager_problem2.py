import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from candidate_manager_problem2 import (
    SceneBEvaluationCache,
    evaluation_key,
    pad_plan_with_empty_cores,
    per_core_utilization,
    run_problem2_candidate_group,
)
from run_problem2_baseline import (
    _aggregate_winners,
    _discover_cases,
    _group_artifacts_intact,
)


def _small_graph():
    return {
        "ops": [
            {"id": 1, "op": "ADD", "pipe": "PIPE_V", "cycles": 10},
            {"id": 2, "op": "MUL", "pipe": "PIPE_M", "cycles": 12},
            {"id": 3, "op": "ADD", "pipe": "PIPE_V", "cycles": 8},
        ],
        "tensors": [
            {"id": 100, "pos": "UB", "size": 32},
            {"id": 101, "pos": "UB", "size": 32},
        ],
        "edges": [
            {"source": 1, "target": 100},
            {"source": 100, "target": 2},
            {"source": 2, "target": 101},
            {"source": 101, "target": 3},
        ],
    }


def _fake_scene_b_result(plan, makespan=None):
    cores = len(plan["core_schedules"])
    subgraphs = len(set(plan["node_to_subgraph"].values()))
    makespan = makespan or (100 + subgraphs)
    timelines = []
    for core_id in range(cores):
        if core_id == 0:
            ops = [{
                "op_id": 1,
                "pipe": "PIPE_V",
                "start": 0,
                "end": makespan,
            }]
            tasks = [{"start": 0, "end": makespan}]
        else:
            ops, tasks = [], [{"start": 0, "end": 0}]
        timelines.append({
            "core_id": core_id,
            "ops": ops,
            "tasks": tasks,
        })
    return {
        "scene": "B",
        "makespan": makespan,
        "data_movement_bytes": {
            "original_graph_copy_bytes": 0,
            "scheduled_copy_bytes": subgraphs,
            "added_copy_bytes": subgraphs,
            "partition_added_copy_bytes": subgraphs,
            "spill_added_copy_bytes": 0,
        },
        "cross_task_traffic": 0,
        "cross_core_transfers": [],
        "memory_peak_by_core": {
            str(core): {"L1": 0, "UB": 32} for core in range(cores)
        },
        "per_core_timeline": timelines,
    }


class Problem2CandidateManagerTests(unittest.TestCase):
    def test_evaluation_key_binds_every_required_identity(self):
        base = {
            "graph_hash": "g",
            "config_hash": "c",
            "plan_hash": "p",
            "canonical_signature": "s",
            "evaluator_hash": "e",
            "solver_hash": "solver",
            "implementation_hash": "impl",
        }
        original = evaluation_key(**base)
        for name in base:
            changed = dict(base)
            changed[name] += "-changed"
            self.assertNotEqual(original, evaluation_key(**changed), name)

    def test_empty_core_inheritance_preserves_source_exactly(self):
        source = {
            "node_to_subgraph": {1: 7, 2: 9},
            "core_schedules": [[7], [9]],
        }
        inherited = pad_plan_with_empty_cores(source, 5)
        self.assertEqual(inherited["node_to_subgraph"], source["node_to_subgraph"])
        self.assertEqual(inherited["core_schedules"][:2], source["core_schedules"])
        self.assertEqual(inherited["core_schedules"][2:], [[], [], []])
        inherited["core_schedules"][0].append(99)
        self.assertEqual(source["core_schedules"], [[7], [9]])

    def test_utilization_uses_union_and_per_pipe_busy_time(self):
        result = {
            "makespan": 20,
            "per_core_timeline": [{
                "core_id": 0,
                "tasks": [{"start": 0, "end": 15}],
                "ops": [
                    {"start": 0, "end": 10, "pipe": "PIPE_M"},
                    {"start": 5, "end": 15, "pipe": "PIPE_V"},
                ],
            }],
        }
        record = per_core_utilization(result)[0]
        self.assertEqual(record["active_any_pipe_cycles"], 15)
        self.assertEqual(record["active_any_pipe_fraction"], 0.75)
        self.assertEqual(record["pipe_busy_cycles"]["PIPE_M"], 10)
        self.assertEqual(record["pipe_utilization"]["PIPE_V"], 0.5)

    def test_group_deduplicates_and_reuses_strict_cache(self):
        calls = []

        def evaluator(graph, plan, config):
            calls.append(1)
            return _fake_scene_b_result(plan)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_path = root / "case_test.json"
            config_path = root / "config.txt"
            graph_path.write_text(json.dumps(_small_graph()), encoding="utf-8")
            config_path.write_text("test", encoding="utf-8")
            first = run_problem2_candidate_group(
                graph_path,
                2,
                config_path=config_path,
                output_root=root / "out",
                cache_dir=root / "cache",
                evaluator=evaluator,
                evaluator_hash_override="fake-evaluator-v1",
                implementation_hash_override="test-implementation-v1",
            )
            unique = first.manifest["unique_legal_plans"]
            self.assertEqual(len(calls), unique)
            self.assertEqual(first.manifest["legal_candidates"], 6)
            self.assertLess(unique, 6)

            calls.clear()
            second = run_problem2_candidate_group(
                graph_path,
                2,
                config_path=config_path,
                output_root=root / "out",
                cache_dir=root / "cache",
                evaluator=evaluator,
                evaluator_hash_override="fake-evaluator-v1",
                implementation_hash_override="test-implementation-v1",
            )
            self.assertEqual(calls, [])
            self.assertEqual(second.manifest["cache_hits"], unique)
            self.assertEqual(first.winner_plan, second.winner_plan)

    def test_evaluation_failure_is_isolated_and_recorded(self):
        def evaluator(graph, plan, config):
            if len(set(plan["node_to_subgraph"].values())) > 1:
                raise RuntimeError("synthetic candidate failure")
            return _fake_scene_b_result(plan, makespan=100)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_path = root / "case_test.json"
            config_path = root / "config.txt"
            graph_path.write_text(json.dumps(_small_graph()), encoding="utf-8")
            config_path.write_text("test", encoding="utf-8")
            result = run_problem2_candidate_group(
                graph_path,
                2,
                config_path=config_path,
                output_root=root / "out",
                cache_dir=root / "cache",
                evaluator=evaluator,
                evaluator_hash_override="failing-fake-evaluator",
                implementation_hash_override="test-implementation-v1",
            )
            failed = [item for item in result.manifest["candidates"]
                      if item["status"] == "failed"]
            self.assertTrue(failed)
            self.assertTrue(all(item["failure_stage"] == "evaluation"
                                for item in failed))
            self.assertEqual(result.winner_name, "single")
            for item in failed:
                record = (
                    root / "out" / "groups" / "case_test" / "k2"
                    / "candidate_records" / (item["name"] + ".json"))
                self.assertTrue(record.is_file())

    def test_cache_rejects_result_tampering(self):
        identity = {
            "evaluation_key": "key",
            "graph_sha256": "g",
            "config_sha256": "c",
            "plan_sha256": "p",
            "canonical_signature": "s",
            "evaluator_sha256": "e",
            "solver_sha256": "solver",
            "problem2_implementation_sha256": "impl",
        }
        result = _fake_scene_b_result({
            "node_to_subgraph": {1: 0}, "core_schedules": [[0]]})
        with tempfile.TemporaryDirectory() as temporary:
            cache = SceneBEvaluationCache(Path(temporary))
            entry = cache.store(identity, result, {}, 0.1, {})
            self.assertIsNotNone(cache.lookup(identity))
            Path(entry["resolved_result_path"]).write_text("{}", encoding="utf-8")
            self.assertIsNone(cache.lookup(identity))

    def test_partial_smoke_aggregate_does_not_report_unrequested_cores(self):
        rows = [
            {"cores": 1, "speedup": 1.0, "makespan_cycles": 100},
            {"cores": 2, "speedup": 2.0, "makespan_cycles": 50},
        ]
        aggregate = _aggregate_winners(rows, expected_cases=1, core_counts=(1, 2))
        self.assertEqual([row["cores"] for row in aggregate], [1, 2])
        self.assertTrue(all(row["failure_count"] == 0 for row in aggregate))

    def test_resumed_group_requires_every_traceability_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group_path = root / "groups" / "case_test" / "k2.json"
            run_dir = group_path.parent / "k2"
            plan = {"node_to_subgraph": {"1": 0}, "core_schedules": [[0], []]}
            result = _fake_scene_b_result(plan)
            files = {
                "final_multicore_res.json": plan,
                "final_official_evaluation.json": result,
                "plans/single_multicore_res.json": plan,
                "official_results/single.json": result,
                "candidate_records/single.json": {"candidate": "single"},
            }
            for relative, value in files.items():
                path = run_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding="utf-8")
            from candidate_manager_problem1 import sha256_file
            group = {
                "final_plan_file_sha256": sha256_file(
                    run_dir / "final_multicore_res.json"),
                "final_official_result_file_sha256": sha256_file(
                    run_dir / "final_official_evaluation.json"),
                "final_official_result_json_sha256": (
                    __import__("candidate_manager_problem2").json_sha256(result)),
                "candidate_record_file_sha256": {
                    "single": sha256_file(
                        run_dir / "candidate_records/single.json"),
                },
                "candidates": [{
                    "name": "single",
                    "status": "evaluated",
                    "canonical_candidate": "single",
                    "plan_file_sha256": sha256_file(
                        run_dir / "plans/single_multicore_res.json"),
                    "official_result_file_sha256": sha256_file(
                        run_dir / "official_results/single.json"),
                    "official_result_json_sha256": (
                        __import__("candidate_manager_problem2").json_sha256(result)),
                }],
            }
            self.assertTrue(_group_artifacts_intact(group, group_path, 2))
            (run_dir / "official_results/single.json").unlink()
            self.assertFalse(_group_artifacts_intact(group, group_path, 2))

    def test_case_discovery_ignores_evaluator_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            for name in (
                "case_001.json",
                "case_100.json",
                "case_000.json",
                "case_101.json",
                "case_001_problem_2_res.json",
                "case_001_problem_2_trace.json",
                "case_001_multicore_res.json",
            ):
                (data / name).write_text("{}", encoding="utf-8")
            self.assertEqual(_discover_cases(root), ["case_001", "case_100"])


if __name__ == "__main__":
    unittest.main()
