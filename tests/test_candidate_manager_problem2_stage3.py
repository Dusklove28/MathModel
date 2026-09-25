import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from solver_problem1 import GraphInfo  # registers official code path
from candidate_manager_problem1 import canonical_plan_signature, sha256_file
from candidate_manager_problem2 import (
    json_sha256,
    official_scene_b_hash,
    problem2_implementation_hash,
    validate_and_compact_result,
)
from candidate_manager_problem2_stage3 import (
    cache_key_summary,
    reusable_stage3_group,
    run_stage3_ordering_group,
)
from problem2_identity import plan_json_sha256


def _graph():
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


def _plan():
    return {
        "node_to_subgraph": {1: 0, 2: 1, 3: 2},
        "core_schedules": [[0, 1], [2]],
    }


def _fake_result(plan, makespan):
    timelines = []
    for core, order in enumerate(plan["core_schedules"]):
        ops = ([{"op_id": core + 1, "pipe": "PIPE_V",
                 "start": 0, "end": makespan}] if order else [])
        timelines.append({
            "core_id": core,
            "ops": ops,
            "tasks": [{"start": 0, "end": makespan if order else 0}],
        })
    return {
        "scene": "B",
        "makespan": makespan,
        "data_movement_bytes": {
            "original_graph_copy_bytes": 0,
            "scheduled_copy_bytes": 64,
            "added_copy_bytes": 64,
            "partition_added_copy_bytes": 64,
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
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _make_frozen_inputs(root):
    graph_path = root / "data" / "case_test.json"
    config_path = root / "data" / "config.txt"
    _write_json(graph_path, _graph())
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        (PROJECT_DIR / "data" / "config.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    plan = _plan()
    evaluator_hash, _ = official_scene_b_hash()
    stage1_hash, _ = problem2_implementation_hash()

    baseline = root / "baseline"
    stage1_group_dir = baseline / "groups" / "case_test" / "k2"
    stage1_plan = stage1_group_dir / "plans" / "b0_multicore_res.json"
    stage1_result = stage1_group_dir / "official_results" / "b0.json"
    _write_json(stage1_plan, plan)
    result100 = _fake_result(plan, 100)
    _write_json(stage1_result, result100)
    metrics100 = validate_and_compact_result(result100)
    candidate = {
        "name": "b0",
        "status": "evaluated",
        "legal": True,
        "plan_hash": json_sha256(plan),
        "plan_file_sha256": sha256_file(stage1_plan),
        "canonical_signature": canonical_plan_signature(plan),
        "canonical_candidate": "b0",
        "official_result_file_sha256": sha256_file(stage1_result),
        "official_result_json_sha256": json_sha256(result100),
        "metrics": metrics100,
    }
    stage1_group_path = baseline / "groups" / "case_test" / "k2.json"
    final_plan = stage1_group_dir / "final_multicore_res.json"
    final_result = stage1_group_dir / "final_official_evaluation.json"
    _write_json(final_plan, plan)
    _write_json(final_result, result100)
    stage1_group = {
        "status": "success",
        "case": "case_test",
        "cores": 2,
        "graph_sha256": sha256_file(graph_path),
        "config_sha256": sha256_file(config_path),
        "solver_sha256": sha256_file(PROJECT_DIR / "solver_problem1.py"),
        "evaluator_sha256": evaluator_hash,
        "problem2_implementation_sha256": stage1_hash,
        "candidates": [candidate],
        "winner": {
            "name": "b0",
            "score": [100, 64],
            "metrics": metrics100,
        },
        "final_plan_file_sha256": sha256_file(final_plan),
        "final_official_result_file_sha256": sha256_file(final_result),
        "final_official_result_json_sha256": json_sha256(result100),
    }
    _write_json(stage1_group_path, stage1_group)
    fields = (
        "case", "cores", "status", "winner", "makespan_cycles",
        "added_copy_bytes", "partition_added_copy_bytes",
        "spill_added_copy_bytes", "cross_task_traffic_bytes",
        "cross_core_transfer_count", "used_core_count",
    )
    with (baseline / "selected_results.csv").open(
            "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "case": "case_test", "cores": 2, "status": "success",
            "winner": "b0", **{field: metrics100[field] for field in fields[4:]},
        })

    stage2 = root / "stage2"
    identity = {
        "implementation_sha256": "frozen-stage2-test",
        "mapping_policies": ["locality"],
        "cache": "/external/frozen-cache",
    }
    identity["run_identity_sha256"] = json_sha256(identity)
    _write_json(stage2 / "run_identity.json", identity)
    stage2_dir = stage2 / "groups" / "case_test" / "k2" / "b0"
    map_plan = stage2_dir / "plans" / "map_locality_multicore_res.json"
    diagnostics = stage2_dir / "diagnostics" / "map_locality.json"
    map_result = stage2_dir / "official_results" / "map_locality.json"
    _write_json(map_plan, plan)
    _write_json(diagnostics, {"policy": "locality"})
    result90 = _fake_result(plan, 90)
    _write_json(map_result, result90)
    metrics90 = validate_and_compact_result(result90)
    mapped = {
        "name": "map_locality",
        "policy": "locality",
        "status": "evaluated",
        "legal": True,
        "plan_hash": "legacy-in-memory-plan-hash",
        "plan_file_sha256": sha256_file(map_plan),
        "diagnostics_file_sha256": sha256_file(diagnostics),
        "canonical_signature": canonical_plan_signature(plan),
        "canonical_candidate": "map_locality",
        "official_result_file_sha256": sha256_file(map_result),
        "official_result_json_sha256": json_sha256(result90),
        "metrics": metrics90,
    }
    stage2_group_path = stage2 / "groups" / "case_test" / "k2" / "b0.json"
    _write_json(stage2_group_path, {
        "schema_version": 2,
        "kind": "problem2_stage2_fixed_partition_mapping_group",
        "status": "success",
        "case": "case_test",
        "cores": 2,
        "partition_family": "b0",
        "graph_sha256": sha256_file(graph_path),
        "config_sha256": sha256_file(config_path),
        "evaluator_sha256": evaluator_hash,
        "problem2_stage2_implementation_sha256": "frozen-stage2-test",
        "candidates": [mapped],
    })
    return graph_path, config_path, baseline, stage2


class Problem2Stage3CandidateManagerTests(unittest.TestCase):
    def test_group_keeps_mapping_reports_both_deltas_and_is_resumable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_path, config_path, baseline, stage2 = _make_frozen_inputs(root)
            calls = []

            def evaluator(graph, plan, config):
                calls.append(plan)
                return _fake_result(plan, 80)

            result = run_stage3_ordering_group(
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                stage2_root=stage2,
                output_root=root / "out",
                cache_dir=root / "cache",
                cores=2,
                family="b0",
                evaluator=evaluator,
            )
            manifest = result.manifest
            self.assertLessEqual(manifest["official_evaluations"], 2)
            self.assertEqual(manifest["failed_ordering_candidates"], 0)
            self.assertEqual(manifest["legal_ordering_candidates"], 2)
            self.assertEqual(
                manifest["delta_vs_fixed_mapping_original_order"][
                    "makespan_cycles"], -10)
            self.assertEqual(
                manifest["final_case_core"][
                    "delta_vs_stage1_global_winner"]["makespan_cycles"], -20)
            self.assertTrue(manifest["final_case_core"][
                "ordering_brought_final_case_core_improvement"])
            winner_name = manifest["winner_within_fixed_mapping"]["name"]
            winner_record = next(
                record for record in manifest["candidates"]
                if record["name"] == winner_name)
            self.assertEqual(
                manifest["winner_within_fixed_mapping"]["plan_hash"],
                winner_record["plan_hash"],
            )
            self.assertEqual(
                plan_json_sha256(calls[0]),
                plan_json_sha256(json.loads(json.dumps(calls[0]))),
            )
            keys = cache_key_summary([manifest])
            self.assertEqual(keys["unique_evaluation_keys"], len(calls))
            self.assertIsNotNone(reusable_stage3_group(
                group_path=result.group_path,
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                stage2_root=stage2,
                cores=2,
                family="b0",
            ))
            release_diagnostics = (
                result.group_path.parent / "b0" / "diagnostics"
                / "order_release.json")
            release_diagnostics.write_text("{}", encoding="utf-8")
            self.assertIsNone(reusable_stage3_group(
                group_path=result.group_path,
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                stage2_root=stage2,
                cores=2,
                family="b0",
            ))

    def test_failed_representative_is_not_propagated_or_resumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_path, config_path, baseline, stage2 = _make_frozen_inputs(root)

            def evaluator(graph, plan, config):
                raise RuntimeError("synthetic transient evaluator failure")

            result = run_stage3_ordering_group(
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                stage2_root=stage2,
                output_root=root / "out",
                cache_dir=root / "cache",
                cores=2,
                family="b0",
                evaluator=evaluator,
            )
            manifest = result.manifest
            self.assertEqual(manifest["status"], "partial_failure")
            self.assertGreater(manifest["failed_ordering_candidates"], 0)
            failed = [record for record in manifest["candidates"]
                      if record.get("status") == "failed"]
            self.assertTrue(failed)
            self.assertTrue(all(record.get("metrics") is None for record in failed))
            self.assertIsNone(reusable_stage3_group(
                group_path=result.group_path,
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                stage2_root=stage2,
                cores=2,
                family="b0",
            ))


if __name__ == "__main__":
    unittest.main()

