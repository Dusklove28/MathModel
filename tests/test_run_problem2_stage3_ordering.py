import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from run_problem2_stage3_ordering import GATE_A_TARGETS, summarize


def _metrics(makespan):
    return {
        "makespan_cycles": makespan,
        "added_copy_bytes": 100,
        "spill_added_copy_bytes": 0,
        "cross_task_traffic_bytes": 10,
        "cross_core_transfer_count": 1,
    }


def _group(case, cores, family, index):
    fixed = _metrics(90)
    stage1 = _metrics(80)
    candidates = [{
        "name": "fixed_mapping_original_order",
        "policy": None,
        "status": "baseline_reference",
        "legal": True,
        "deduplicated": False,
        "canonical_candidate": "fixed_mapping_original_order",
        "cache_hit": False,
        "evaluation_key": None,
        "plan_hash": "fixed-{}".format(index),
        "metrics": fixed,
        "timing": {},
    }]
    for policy in ("release", "critical"):
        candidates.append({
            "name": "order_{}".format(policy),
            "policy": policy,
            "status": "evaluated",
            "legal": True,
            "deduplicated": False,
            "canonical_candidate": "order_{}".format(policy),
            "cache_hit": False,
            "evaluation_key": "key-{}-{}".format(index, policy),
            "plan_hash": "plan-{}-{}".format(index, policy),
            "official_result_json_sha256": "result-{}-{}".format(index, policy),
            "metrics": _metrics(85),
            "timing": {},
        })
    return {
        "case": case,
        "cores": cores,
        "partition_family": family,
        "fixed_mapping_reference": {
            "candidate": "map_locality",
            "metrics": fixed,
        },
        "stage1_global_reference": {
            "winner": "stage1",
            "metrics": stage1,
        },
        "best_new_ordering": {
            "name": "order_release",
            "metrics": _metrics(85),
        },
        "winner_within_fixed_mapping": {
            "name": "order_release",
            "metrics": _metrics(85),
        },
        "delta_vs_fixed_mapping_original_order": {
            "makespan_cycles": -5,
            "added_copy_bytes": 0,
            "spill_added_copy_bytes": 0,
        },
        "final_case_core": {
            "source": "stage1_global_fallback",
            "winner": "stage1",
            "metrics": stage1,
            "delta_vs_stage1_global_winner": {
                "makespan_cycles": 0,
                "added_copy_bytes": 0,
                "spill_added_copy_bytes": 0,
            },
            "ordering_brought_final_case_core_improvement": False,
        },
        "candidates": candidates,
        "official_evaluations": 2,
        "cache_hits": 0,
        "failed_ordering_candidates": 0,
    }


class Problem2Stage3RunnerTests(unittest.TestCase):
    def _groups(self):
        return [
            _group(case, cores, family, index)
            for index, (case, cores, family) in enumerate(GATE_A_TARGETS)
        ]

    def test_zero_final_improvements_stops_ordering_expansion(self):
        with tempfile.TemporaryDirectory() as temporary:
            summary = summarize(
                output=Path(temporary),
                groups=self._groups(),
                failures=[],
                run_identity={"run_identity_sha256": "identity"},
                wall_seconds=1.0,
            )
            self.assertTrue(summary["technical_gate"]["passed"])
            self.assertEqual(summary["official_evaluations"], 6)
            self.assertEqual(
                summary["decision_rule"]["recommendation"],
                "stop_ordering_expansion_and_prioritize_problem3",
            )
            self.assertFalse(summary["decision_rule"]["blind_cases_used"])
            cache_summary = json.loads((
                Path(temporary) / "cache_key_summary.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(cache_summary["unique_evaluation_keys"], 6)

    def test_any_gain_requires_development_distribution_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            groups = self._groups()
            improved = copy.deepcopy(groups[0]["final_case_core"])
            improved.update({
                "source": "stage3_fixed_mapping_portfolio",
                "winner": "order_release",
                "metrics": _metrics(70),
                "delta_vs_stage1_global_winner": {
                    "makespan_cycles": -10,
                    "added_copy_bytes": 0,
                    "spill_added_copy_bytes": 0,
                },
                "ordering_brought_final_case_core_improvement": True,
            })
            groups[0]["final_case_core"] = improved
            summary = summarize(
                output=Path(temporary),
                groups=groups,
                failures=[],
                run_identity={"run_identity_sha256": "identity"},
                wall_seconds=1.0,
            )
            self.assertEqual(
                summary["final_case_core_improvements_brought_by_ordering"], 1)
            self.assertEqual(summary["improvement_case_count"], 1)
            self.assertIn(
                "development_set_before_any_blind_case",
                summary["decision_rule"]["recommendation"],
            )


if __name__ == "__main__":
    unittest.main()

