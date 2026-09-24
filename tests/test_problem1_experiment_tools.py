from __future__ import annotations

import unittest

from analyze_problem1_development import METHODS, aggregate_by_case, build_ablation
from run_problem1_full import _manifest_matches_development


def _candidate(name: str, score: tuple[int, int], *, winner: bool = False):
    return {
        "name": name,
        "method": name.upper(),
        "parameters": {},
        "status": "evaluated",
        "error": None,
        "canonical_signature": name,
        "canonical_candidate": name,
        "equivalent_methods": [name],
        "deduplicated": False,
        "generation_time": 0.0,
        "validation_time": 0.0,
        "evaluation_time": 1.0,
        "cache_lookup_time": 0.0,
        "cache_hit": False,
        "evaluation_key": name,
        "makespan": score[0],
        "added_copy_bytes": score[1],
        "max_memory_bytes": 1,
        "full_result_path": name + ".json",
    }


def _manifest(case: str, cores: int, scores: dict[str, tuple[int, int]]):
    records = [_candidate(name, scores[name]) for name in METHODS]
    priority = {name: index for index, name in enumerate(METHODS)}
    winner = min(
        records,
        key=lambda row: (
            row["makespan"], row["added_copy_bytes"], priority[row["name"]]),
    )
    return {
        "case": case,
        "cores": cores,
        "candidate_priority": list(METHODS),
        "fatal_error": None,
        "winner": {
            "name": winner["name"],
            "makespan": winner["makespan"],
            "added_copy_bytes": winner["added_copy_bytes"],
        },
        "candidates": records,
    }


class DevelopmentAblationTests(unittest.TestCase):
    def test_classifies_unique_makespan_movement_and_exact_ties(self):
        manifests = [
            {
                "case": "case_001",
                "cores": 2,
                "manifest": _manifest("case_001", 2, {
                    "single": (100, 0), "b0": (90, 20), "b1": (90, 20),
                    "b2a_w4": (80, 40), "b2a_w8": (80, 60),
                    "b2a_w16": (81, 30),
                }),
            },
            {
                "case": "case_001",
                "cores": 3,
                "manifest": _manifest("case_001", 3, {
                    "single": (100, 0), "b0": (70, 20), "b1": (70, 20),
                    "b2a_w4": (90, 30), "b2a_w8": (91, 40),
                    "b2a_w16": (92, 50),
                }),
            },
            {
                "case": "case_002",
                "cores": 2,
                "manifest": _manifest("case_002", 2, {
                    "single": (60, 0), "b0": (70, 20), "b1": (71, 20),
                    "b2a_w4": (72, 30), "b2a_w8": (73, 40),
                    "b2a_w16": (74, 50),
                }),
            },
        ]
        _, details, by_method = build_ablation(manifests)
        lookup = {
            (row["case"], row["cores"], row["omitted_method"]): row
            for row in details
        }
        self.assertEqual(
            lookup[("case_001", 2, "b2a_w4")]["classification"],
            "movement_only_improvement",
        )
        self.assertEqual(
            lookup[("case_001", 3, "b0")]["classification"], "exact_tie")
        self.assertEqual(
            lookup[("case_001", 3, "b1")]["classification"], "exact_tie")
        self.assertEqual(
            lookup[("case_002", 2, "single")]["classification"],
            "strict_makespan_improvement",
        )
        single = next(row for row in by_method if row["method"] == "single")
        self.assertEqual(single["strict_makespan_improvement"], 1)
        case_rows = aggregate_by_case(details)
        self.assertEqual(len(case_rows), 2 * len(METHODS))

    def test_development_manifest_match_checks_metrics_not_winner_count(self):
        manifest = _manifest("case_001", 4, {
            "single": (100, 0), "b0": (80, 20), "b1": (80, 20),
            "b2a_w4": (90, 30), "b2a_w8": (91, 40),
            "b2a_w16": (92, 50),
        })
        row = {
            "case": "case_001", "cores": 4, "winner": "b0",
            "winner_makespan": 80, "winner_added_bytes": 20,
        }
        self.assertTrue(_manifest_matches_development(manifest, row))
        row["winner_makespan"] = 81
        self.assertFalse(_manifest_matches_development(manifest, row))


if __name__ == "__main__":
    unittest.main()
