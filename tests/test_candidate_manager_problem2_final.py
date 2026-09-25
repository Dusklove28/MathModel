import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from candidate_manager_problem1 import sha256_file
from candidate_manager_problem2 import json_sha256, validate_and_compact_result
from candidate_manager_problem2_final import (
    FinalCandidate,
    inherited_candidate,
    mapping_candidate,
    reusable_final_group,
)
from problem2_identity import plan_json_sha256
from run_problem2_final import EXPECTED_SPLIT_LIST_HASH, _load_split


def _graph():
    return {
        "ops": [
            {"id": 1, "op": "ADD", "pipe": "PIPE_V", "cycles": 10},
            {"id": 2, "op": "MUL", "pipe": "PIPE_M", "cycles": 12},
        ],
        "tensors": [
            {"id": 100, "pos": "UB", "size": 32},
        ],
        "edges": [
            {"source": 1, "target": 100},
            {"source": 100, "target": 2},
        ],
    }


def _plan():
    return {
        "node_to_subgraph": {1: 0, 2: 1},
        "core_schedules": [[0], [1]],
    }


def _fake_result(plan, makespan=100, added=64):
    timelines = []
    for core, order in enumerate(plan["core_schedules"]):
        timelines.append({
            "core_id": core,
            "ops": ([{
                "op_id": core + 1,
                "pipe": "PIPE_V",
                "start": 0,
                "end": makespan,
            }] if order else []),
            "tasks": [{"start": 0, "end": makespan if order else 0}],
        })
    return {
        "scene": "B",
        "makespan": makespan,
        "data_movement_bytes": {
            "original_graph_copy_bytes": 0,
            "scheduled_copy_bytes": added,
            "added_copy_bytes": added,
            "partition_added_copy_bytes": added,
            "spill_added_copy_bytes": 0,
        },
        "cross_task_traffic": 32,
        "cross_core_transfers": [{"size": 32}],
        "memory_peak_by_core": {
            str(core): {"L1": 0, "UB": 32}
            for core in range(len(plan["core_schedules"]))
        },
        "per_core_timeline": timelines,
    }


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class Problem2FinalCandidateManagerTests(unittest.TestCase):
    def test_frozen_split_covers_all_official_cases(self):
        split = _load_split(PROJECT_DIR / "problem2_preregistered_split.json")
        cases = (
            split["development_cases"]
            + split["validation_cases"]
            + split["holdout_cases"]
        )
        self.assertEqual(len(cases), 100)
        self.assertEqual(len(set(cases)), 100)
        self.assertEqual(
            split["case_lists_canonical_sha256"], EXPECTED_SPLIT_LIST_HASH)

    def test_score_uses_makespan_then_added_copy_then_stable_priority(self):
        first = FinalCandidate(
            name="first", family="test", priority=0, legal=True,
            metrics={"makespan_cycles": 100, "added_copy_bytes": 80},
        )
        second = FinalCandidate(
            name="second", family="test", priority=1, legal=True,
            metrics={"makespan_cycles": 100, "added_copy_bytes": 64},
        )
        third = FinalCandidate(
            name="third", family="test", priority=2, legal=True,
            metrics={"makespan_cycles": 99, "added_copy_bytes": 999},
        )
        self.assertEqual(min((first, second, third), key=lambda item: item.score()),
                         third)
        self.assertLess(second.score(), first.score())

    def test_inherited_candidate_cache_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.txt"
            config_path.write_text("test", encoding="utf-8")
            calls = []

            def evaluator(graph, plan, config):
                calls.append((graph, plan, config))
                return _fake_result(plan, makespan=90)

            kwargs = {
                "graph_json": _graph(),
                "graph_hash": "1" * 64,
                "config_path": config_path,
                "config_hash": "2" * 64,
                "source_plan": _plan(),
                "source_cores": 2,
                "target_cores": 3,
                "source_group_hash": "3" * 64,
                "priority": 6,
                "cache_dir": root / "cache",
                "implementation_hash": "4" * 64,
                "solver_hash": "5" * 64,
                "existing_by_signature": {},
            }
            with patch(
                "candidate_manager_problem2_final.official_scene_b_hash",
                return_value=("6" * 64, {}),
            ), patch(
                "candidate_manager_problem2_final.evaluate_official_problem2",
                side_effect=evaluator,
            ):
                first = inherited_candidate(**kwargs)
                second = inherited_candidate(**kwargs)

            self.assertTrue(first.legal)
            self.assertEqual(first.status, "evaluated")
            self.assertFalse(first.cache_hit)
            self.assertEqual(second.status, "evaluated")
            self.assertTrue(second.cache_hit)
            self.assertEqual(second.timing["official_evaluation_seconds"], 0.0)
            self.assertEqual(first.evaluation_key, second.evaluation_key)
            self.assertEqual(len(calls), 1)
            self.assertTrue(Path(second.official_result_path).is_file())

    def test_mapping_candidate_accepts_verified_stage2_deduplication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group_path = root / "b0.json"
            group_dir = root / "b0"
            plan_path = group_dir / "plans" / "map_locality_multicore_res.json"
            result_path = root / "baseline_result.json"
            plan = _plan()
            result = _fake_result(plan)
            _write_json(plan_path, plan)
            _write_json(result_path, result)
            record = {
                "name": "map_locality",
                "status": "deduplicated",
                "legal": True,
                "canonical_candidate": "original",
                "plan_hash": plan_json_sha256(plan),
                "plan_file_sha256": sha256_file(plan_path),
                "official_result_path": str(result_path),
                "official_result_json_sha256": json_sha256(result),
                "metrics": validate_and_compact_result(result),
                "timing": {},
            }
            record["metrics"]["memory_peak_by_core"] = {
                int(key): value
                for key, value in record["metrics"]["memory_peak_by_core"].items()
            }
            group = {
                "candidates": [record],
                "baseline": {"official_result_path": str(result_path)},
            }
            _write_json(group_path, group)
            candidate = mapping_candidate(
                group=group,
                group_dir=group_dir,
                family="b0",
                graph_json=_graph(),
                priority=1,
            )
            self.assertTrue(candidate.legal)
            self.assertEqual(
                candidate.status, "verified_stage2_deduplicated")
            self.assertEqual(candidate.metrics["makespan_cycles"], 100)

    def test_resume_rejects_tampered_final_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group_dir = root / "groups" / "case_test" / "k2"
            group_path = root / "groups" / "case_test" / "k2.json"
            plan_path = group_dir / "final_multicore_res.json"
            result_path = group_dir / "final_official_evaluation.json"
            inherited_path = (
                group_dir / "candidate_plans"
                / "inherit_final_k1_multicore_res.json")
            plan = _plan()
            result = _fake_result(plan)
            _write_json(plan_path, plan)
            _write_json(result_path, result)
            _write_json(inherited_path, plan)
            identity = "7" * 64
            manifest = {
                "schema_version": 1,
                "kind": "problem2_final_portfolio_group",
                "status": "success",
                "case": "case_test",
                "cores": 2,
                "run_identity_sha256": identity,
                "final_plan_file_sha256": sha256_file(plan_path),
                "final_official_result_file_sha256": sha256_file(result_path),
                "final_plan_hash": plan_json_sha256(plan),
                "final_official_result_json_sha256": json_sha256(result),
                "winner": {"metrics": validate_and_compact_result(result)},
                "mapping_group_file_sha256": {},
                "candidates": [{
                    "name": "inherit_final_k1",
                    "family": "recursive_final_winner_inheritance",
                    "legal": True,
                    "plan_hash": plan_json_sha256(plan),
                    "plan_file_sha256": sha256_file(inherited_path),
                }],
            }
            _write_json(group_path, manifest)
            self.assertIsNotNone(reusable_final_group(
                group_path=group_path,
                run_identity_sha256=identity,
                case="case_test",
                cores=2,
            ))
            inherited_path.write_text("{}", encoding="utf-8")
            self.assertIsNone(reusable_final_group(
                group_path=group_path,
                run_identity_sha256=identity,
                case="case_test",
                cores=2,
            ))
            _write_json(inherited_path, plan)
            result["makespan"] += 1
            _write_json(result_path, result)
            self.assertIsNone(reusable_final_group(
                group_path=group_path,
                run_identity_sha256=identity,
                case="case_test",
                cores=2,
            ))


if __name__ == "__main__":
    unittest.main()
