import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from run_problem2_stage2_mapping import (
    _discover_cases,
    _process_exit_code,
    _run_cell,
    _validate_preset_budget,
)


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
            _validate_preset_budget("stratified12")
        _validate_preset_budget("gate-b")

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


if __name__ == "__main__":
    unittest.main()

