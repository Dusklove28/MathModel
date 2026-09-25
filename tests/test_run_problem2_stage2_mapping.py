import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from run_problem2_stage2_mapping import (
    GATE_B_CASES,
    GATE_B_CORES,
    STRATIFIED_12_CASES,
    _case_core_rows,
    _discover_cases,
    _policy_case_core_rows,
    _process_exit_code,
    _report_stratum,
    _resolve_mapping_policies,
    _run_cell,
    _validate_preset_budget,
)
from candidate_manager_problem2_stage2 import Stage1SelectedReference


class Problem2Stage2RunnerTests(unittest.TestCase):
    def test_custom_case_discovery_accepts_only_official_case_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            data = repo / "data"
            data.mkdir()
            for name in (
                "case_001.json",
                "case_100.json",
                "case_001_problem_2_trace.json",
                "case_notes.json",
            ):
                (data / name).write_text("{}", encoding="utf-8")
            self.assertEqual(_discover_cases(repo), ("case_001", "case_100"))

    def test_stratified12_is_locked_before_policy_freeze(self):
        with self.assertRaisesRegex(ValueError, "budget-locked"):
            _validate_preset_budget("stratified12", ("critical", "locality", "memory"))
        _validate_preset_budget("stratified12", ("locality",))
        _validate_preset_budget("gate-b", ("critical", "locality", "memory"))
        self.assertEqual(_resolve_mapping_policies("map_locality"), ("locality",))

    def test_scientific_gate_failure_has_distinct_nonzero_exit(self):
        self.assertEqual(_process_exit_code({
            "complete": True,
            "continue_gate": {"passed": True},
        }, []), 0)
        self.assertEqual(_process_exit_code({
            "complete": True,
            "continue_gate": {"passed": False},
        }, []), 2)
        self.assertEqual(_process_exit_code({
            "complete": False,
            "continue_gate": {"passed": False},
        }, []), 1)

    def test_one_family_failure_does_not_skip_later_family(self):
        payload = {
            "repo": "/repo",
            "baseline": "/baseline",
            "output": "/output",
            "cache": "/cache",
            "case": "case_001",
            "cores": 2,
            "families": ["b0", "b1"],
            "mapping_policies": ["locality"],
            "max_moves": 2,
            "search_width": 12,
        }
        success = SimpleNamespace(manifest={
            "case": "case_001",
            "cores": 2,
            "partition_family": "b1",
            "winner": {"name": "original"},
        })
        with patch(
            "run_problem2_stage2_mapping.reusable_stage2_group",
            return_value=None,
        ), patch(
            "run_problem2_stage2_mapping.run_stage2_mapping_group",
            side_effect=[RuntimeError("synthetic b0 failure"), success],
        ) as runner:
            found = _run_cell(payload)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(len(found["groups"]), 1)
        self.assertEqual(found["groups"][0]["partition_family"], "b1")
        self.assertEqual(len(found["failures"]), 1)
        self.assertEqual(found["failures"][0]["partition_family"], "b0")

    def test_stratified12_reporting_strata_are_pre_registered(self):
        counts = {name: 0 for name in (
            "repeated_gate_b", "seen_case_new_core", "unseen_case")}
        for case in STRATIFIED_12_CASES:
            for cores in (2, 3, 4, 5):
                counts[_report_stratum(case, cores)] += 5
        self.assertEqual(counts, {
            "repeated_gate_b": 45,
            "seen_case_new_core": 15,
            "unseen_case": 180,
        })
        self.assertEqual(len(GATE_B_CASES) * len(GATE_B_CORES), 9)

    def test_case_core_uses_stage1_actual_winner_and_score_tuple(self):
        def metrics(makespan, added, spill=0):
            return {
                "makespan_cycles": makespan,
                "added_copy_bytes": added,
                "spill_added_copy_bytes": spill,
            }

        reference = Stage1SelectedReference(
            case="case_100", cores=5, winner="inherit_k4",
            metrics=metrics(67632, 1615378, 229376),
            group_path=Path("group"), group_file_hash="g",
            plan_path=Path("plan"), plan_file_hash="p",
            official_result_path=Path("result"), official_result_json_hash="r",
        )
        groups = [{
            "case": "case_100",
            "cores": 5,
            "partition_family": "b2a_w4",
            "candidates": [
                {"name": "original", "legal": True,
                 "metrics": metrics(68212, 1500000)},
                {"name": "map_locality", "policy": "locality", "legal": True,
                 "metrics": metrics(65477, 1487540, 100000)},
            ],
        }]
        selected = {("case_100", 5): reference}
        row = _case_core_rows(groups, selected)[0]
        self.assertEqual(row["stage1_winner"], "inherit_k4")
        self.assertEqual(row["stage1_makespan_cycles"], 67632)
        self.assertEqual(row["final_makespan_cycles"], 65477)
        self.assertEqual(row["makespan_delta_cycles"], -2155)
        policy_row = _policy_case_core_rows(
            groups, selected, ("locality",))[0]
        self.assertEqual(policy_row["final_source"], "mapping")

        tie_reference = Stage1SelectedReference(
            case="case_tie", cores=2, winner="single",
            metrics=metrics(100, 100), group_path=Path("group"),
            group_file_hash="g", plan_path=Path("plan"), plan_file_hash="p",
            official_result_path=Path("result"), official_result_json_hash="r",
        )
        tie_groups = [{
            "case": "case_tie", "cores": 2, "partition_family": "b0",
            "candidates": [
                {"name": "original", "legal": True,
                 "metrics": metrics(110, 80)},
                {"name": "map_locality", "policy": "locality", "legal": True,
                 "metrics": metrics(100, 90)},
            ],
        }]
        tie = _case_core_rows(
            tie_groups, {("case_tie", 2): tie_reference})[0]
        self.assertTrue(tie["score_improved"])
        self.assertFalse(tie["makespan_improved"])
        self.assertEqual(tie["final_added_copy_bytes"], 90)


if __name__ == "__main__":
    unittest.main()

