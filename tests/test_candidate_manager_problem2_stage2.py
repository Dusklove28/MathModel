import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from solver_problem1 import GraphInfo  # also registers the official code path
from candidate_manager_problem1 import canonical_plan_signature, sha256_file
from candidate_manager_problem2 import (
    json_sha256,
    official_scene_b_hash,
    problem2_implementation_hash,
    validate_and_compact_result,
)
from candidate_manager_problem2_stage2 import (
    Problem2Stage2Error,
    load_verified_stage1_selected_references,
    load_verified_baseline_reference,
    reusable_stage2_group,
    run_stage2_mapping_group,
)
from solver_problem2 import build_mapping_features


def _graph():
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


def _plan():
    return {
        "node_to_subgraph": {1: 0, 2: 1, 3: 2},
        "core_schedules": [[0, 2], [1]],
    }


def _fake_result(plan, makespan=100):
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


def _make_baseline(root):
    graph_path = root / "data" / "case_test.json"
    config_path = root / "data" / "config.txt"
    _write_json(graph_path, _graph())
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        (PROJECT_DIR / "data" / "config.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    baseline = root / "baseline"
    group_dir = baseline / "groups" / "case_test" / "k2"
    plan_path = group_dir / "plans" / "b0_multicore_res.json"
    result_path = group_dir / "official_results" / "b0.json"
    plan = _plan()
    result = _fake_result(plan)
    _write_json(plan_path, plan)
    _write_json(result_path, result)
    evaluator_hash, _ = official_scene_b_hash()
    stage1_hash, _ = problem2_implementation_hash()
    candidate = {
        "name": "b0",
        "status": "evaluated",
        "legal": True,
        "plan_hash": json_sha256(plan),
        "plan_file_sha256": sha256_file(plan_path),
        "canonical_signature": canonical_plan_signature(plan),
        "canonical_candidate": "b0",
        "official_result_file_sha256": sha256_file(result_path),
        "official_result_json_sha256": json_sha256(result),
        "metrics": validate_and_compact_result(result),
    }
    group = {
        "status": "success",
        "case": "case_test",
        "cores": 2,
        "graph_sha256": sha256_file(graph_path),
        "config_sha256": sha256_file(config_path),
        "solver_sha256": sha256_file(PROJECT_DIR / "solver_problem1.py"),
        "evaluator_sha256": evaluator_hash,
        "problem2_implementation_sha256": stage1_hash,
        "candidates": [candidate],
    }
    group_path = baseline / "groups" / "case_test" / "k2.json"
    _write_json(group_path, group)
    return graph_path, config_path, baseline, plan_path


class Problem2Stage2CandidateManagerTests(unittest.TestCase):
    def test_base_linear_extension_reproduces_original_core_orders(self):
        graph_json = _graph()
        base = _plan()
        features = build_mapping_features(
            graph_json, GraphInfo.from_graph(graph_json), base)
        assignment = {
            sg: core for core, order in enumerate(base["core_schedules"])
            for sg in order
        }
        projected = [[] for _ in base["core_schedules"]]
        for sg in features.global_order:
            projected[assignment[sg]].append(sg)
        self.assertEqual(projected, base["core_schedules"])

    def test_baseline_reference_rejects_plan_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            graph_path, config_path, baseline, plan_path = _make_baseline(
                Path(temporary))
            loaded = load_verified_baseline_reference(
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                cores=2,
                family="b0",
            )
            self.assertEqual(loaded.metrics["makespan_cycles"], 100)
            plan_path.write_text("{}", encoding="utf-8")
            with self.assertRaises(Problem2Stage2Error):
                load_verified_baseline_reference(
                    graph_path=graph_path,
                    config_path=config_path,
                    baseline_root=baseline,
                    cores=2,
                    family="b0",
                )

    def test_selected_results_are_bound_to_stage1_final_official_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_path, config_path, baseline, plan_path = _make_baseline(root)
            group_path = baseline / "groups" / "case_test" / "k2.json"
            group = json.loads(group_path.read_text(encoding="utf-8"))
            group_dir = baseline / "groups" / "case_test" / "k2"
            final_plan = group_dir / "final_multicore_res.json"
            final_result = group_dir / "final_official_evaluation.json"
            final_plan.write_text(plan_path.read_text(encoding="utf-8"),
                                  encoding="utf-8")
            source_result = group_dir / "official_results" / "b0.json"
            final_result.write_text(source_result.read_text(encoding="utf-8"),
                                    encoding="utf-8")
            result = json.loads(final_result.read_text(encoding="utf-8"))
            metrics = validate_and_compact_result(result)
            group["winner"] = {
                "name": "b0",
                "score": [metrics["makespan_cycles"], metrics["added_copy_bytes"]],
                "metrics": metrics,
            }
            group["final_plan_file_sha256"] = sha256_file(final_plan)
            group["final_official_result_file_sha256"] = sha256_file(final_result)
            group["final_official_result_json_sha256"] = json_sha256(result)
            _write_json(group_path, group)
            selected_path = baseline / "selected_results.csv"
            fields = (
                "case", "cores", "status", "winner", "makespan_cycles",
                "added_copy_bytes", "partition_added_copy_bytes",
                "spill_added_copy_bytes", "cross_task_traffic_bytes",
                "cross_core_transfer_count", "used_core_count",
            )
            with selected_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "case": "case_test", "cores": 2, "status": "success",
                    "winner": "b0", **{field: metrics[field] for field in fields[4:]},
                })
            snapshot = load_verified_stage1_selected_references(
                graph_root=graph_path.parent,
                config_path=config_path,
                baseline_root=baseline,
                pairs=(("case_test", 2),),
            )
            self.assertEqual(
                snapshot.references[("case_test", 2)].score(), (100, 64))
            text = selected_path.read_text(encoding="utf-8-sig")
            selected_path.write_text(text.replace(",64,64,", ",65,64,"),
                                     encoding="utf-8-sig")
            with self.assertRaises(Problem2Stage2Error):
                load_verified_stage1_selected_references(
                    graph_root=graph_path.parent,
                    config_path=config_path,
                    baseline_root=baseline,
                    pairs=(("case_test", 2),),
                )

    def test_resume_requires_candidate_diagnostics_and_result_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_path, config_path, baseline, _ = _make_baseline(root)

            def evaluator(graph, plan, config):
                return _fake_result(plan, makespan=90)

            result = run_stage2_mapping_group(
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                output_root=root / "out",
                cache_dir=root / "cache",
                cores=2,
                family="b0",
                max_moves=0,
                search_width=2,
                evaluator=evaluator,
            )
            reusable = reusable_stage2_group(
                group_path=result.group_path,
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                cores=2,
                family="b0",
                max_moves=0,
                search_width=2,
            )
            self.assertIsNotNone(reusable)
            mapped = next(record for record in result.manifest["candidates"]
                          if record["name"] != "original")
            diagnostics = (
                result.group_path.parent / "b0" / "diagnostics"
                / (mapped["name"] + ".json")
            )
            diagnostics.write_text("{}", encoding="utf-8")
            self.assertIsNone(reusable_stage2_group(
                group_path=result.group_path,
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                cores=2,
                family="b0",
                max_moves=0,
                search_width=2,
            ))

    def test_single_policy_is_bound_to_group_identity_and_cache_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_path, config_path, baseline, _ = _make_baseline(root)

            def evaluator(graph, plan, config):
                return _fake_result(plan, makespan=90)

            result = run_stage2_mapping_group(
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                output_root=root / "out",
                cache_dir=root / "cache",
                cores=2,
                family="b0",
                max_moves=1,
                search_width=2,
                mapping_policies=("locality",),
                evaluator=evaluator,
            )
            self.assertEqual(result.manifest["mapping_policies"], ["locality"])
            self.assertEqual(
                [record["name"] for record in result.manifest["candidates"]],
                ["original", "map_locality"],
            )
            mapped = result.manifest["candidates"][1]
            if mapped["status"] == "evaluated":
                entry = json.loads((
                    root / "cache" / "entries"
                    / (mapped["evaluation_key"] + ".json")
                ).read_text(encoding="utf-8"))
                self.assertEqual(entry["metadata"]["mapping_policy"], "locality")
                self.assertEqual(entry["metadata"]["mapping_policies"], ["locality"])
            self.assertIsNotNone(reusable_stage2_group(
                group_path=result.group_path,
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                cores=2,
                family="b0",
                max_moves=1,
                search_width=2,
                mapping_policies=("locality",),
            ))
            self.assertIsNone(reusable_stage2_group(
                group_path=result.group_path,
                graph_path=graph_path,
                config_path=config_path,
                baseline_root=baseline,
                cores=2,
                family="b0",
                max_moves=1,
                search_width=2,
                mapping_policies=("memory",),
            ))


if __name__ == "__main__":
    unittest.main()

