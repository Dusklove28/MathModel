from __future__ import annotations

import unittest

from candidate_manager_problem1 import CandidateManagerError
from run_problem1_inherited_fallback import (
    STORAGE_COMPATIBLE_PREVIOUS_RUNNER_HASHES,
    _aggregate,
    _compatible_runner_revision,
    _resumable_group,
    pad_plan_for_target,
    select_final_option,
)

from pathlib import Path
from tempfile import TemporaryDirectory
import json


class InheritedFallbackTests(unittest.TestCase):
    def test_padding_preserves_partition_and_existing_schedule_order(self):
        source = {
            "node_to_subgraph": {"10": 4, "11": 5},
            "core_schedules": [[4], [5]],
        }
        inherited = pad_plan_for_target(source, 5)
        self.assertEqual(inherited["node_to_subgraph"], source["node_to_subgraph"])
        self.assertEqual(inherited["core_schedules"], [[4], [5], [], [], []])
        self.assertIsNot(inherited["node_to_subgraph"], source["node_to_subgraph"])
        self.assertIsNot(inherited["core_schedules"][0], source["core_schedules"][0])

    def test_padding_rejects_smaller_target(self):
        source = {
            "node_to_subgraph": {"10": 4},
            "core_schedules": [[4], []],
        }
        with self.assertRaises(CandidateManagerError):
            pad_plan_for_target(source, 1)

    def test_original_wins_exact_tie(self):
        winner = select_final_option((100, 20), [{
            "status": "success",
            "name": "inherit_k2",
            "source_cores": 2,
            "measured_makespan": 100,
            "measured_added_copy_bytes": 20,
        }])
        self.assertEqual(winner["name"], "original_target_winner")

    def test_fallback_uses_lexicographic_official_score(self):
        winner = select_final_option((100, 20), [
            {
                "status": "success",
                "name": "inherit_k2",
                "source_cores": 2,
                "measured_makespan": 100,
                "measured_added_copy_bytes": 10,
            },
            {
                "status": "success",
                "name": "inherit_k3",
                "source_cores": 3,
                "measured_makespan": 99,
                "measured_added_copy_bytes": 1000,
            },
        ])
        self.assertEqual(winner["name"], "inherit_k3")

    def test_aggregate_labels_mixed_projection_and_measurement(self):
        rows = [
            {
                "case": "case_001", "cores": 3, "t_i_1": 200,
                "old_speedup": 1.5,
                "pre_reevaluation_projected_speedup": 2.0,
                "final_speedup": 2.0,
                "final_makespan": 100,
                "final_added_copy_bytes": 10,
                "makespan_improvement": 20,
                "officially_measured": True,
            },
            {
                "case": "case_002", "cores": 3, "t_i_1": 300,
                "old_speedup": 1.5,
                "pre_reevaluation_projected_speedup": 3.0,
                "final_speedup": 3.0,
                "final_makespan": 100,
                "final_added_copy_bytes": 20,
                "makespan_improvement": 30,
                "officially_measured": False,
            },
        ]
        row = next(item for item in _aggregate(rows) if item["cores"] == 3)
        self.assertEqual(row["officially_measured_case_count"], 1)
        self.assertEqual(row["projected_only_case_count"], 1)
        self.assertEqual(
            row["reported_value_kind"],
            "mixed_official_measurements_and_pre_reevaluation_projections",
        )

    def test_storage_only_runner_revision_is_compatible(self):
        recorded = {
            "source_frozen_fingerprint": {"solver_sha256": "same"},
            "fallback_runner_sha256": next(iter(
                STORAGE_COMPATIBLE_PREVIOUS_RUNNER_HASHES)),
            "fallback_policy": {"padding": "append_empty_core_schedules"},
        }
        current = {
            **recorded,
            "fallback_runner_sha256": "new-runner",
        }
        self.assertTrue(_compatible_runner_revision(recorded, current))
        unapproved = {**recorded, "fallback_runner_sha256": "unknown-runner"}
        self.assertFalse(_compatible_runner_revision(unapproved, current))
        current["fallback_policy"] = {"padding": "changed"}
        self.assertFalse(_compatible_runner_revision(recorded, current))

    def test_resumable_group_uses_relocated_deterministic_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            record_path = root / "groups" / "case_001" / "k3.json"
            run_dir = record_path.with_suffix("")
            run_dir.mkdir(parents=True)
            final_plan = run_dir / "final_multicore_res.json"
            final_result = run_dir / "final_official_evaluation.json"
            final_plan.write_text("{}", encoding="utf-8")
            final_result.write_text("{}", encoding="utf-8")
            identity = {"frozen": "same"}
            record_path.write_text(json.dumps({
                "status": "success",
                "experiment_identity": identity,
                "final_plan_path": "/old/disk/final_multicore_res.json",
                "final_official_result_path": "/old/disk/result.json",
            }), encoding="utf-8")
            resumed = _resumable_group(record_path, identity)
            self.assertIsNotNone(resumed)
            self.assertEqual(resumed["final_plan_path"], str(final_plan.resolve()))
            self.assertEqual(
                resumed["final_official_result_path"],
                str(final_result.resolve()),
            )


if __name__ == "__main__":
    unittest.main()
