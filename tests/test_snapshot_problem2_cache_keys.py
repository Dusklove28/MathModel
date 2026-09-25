import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from snapshot_problem2_cache_keys import snapshot


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class Problem2CacheKeySnapshotTests(unittest.TestCase):
    def test_snapshot_deduplicates_keys_and_binds_source_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = {
                "cache": "/external/cache",
            }
            from candidate_manager_problem2 import json_sha256
            identity["run_identity_sha256"] = json_sha256(identity)
            _write(root / "run_identity.json", identity)
            _write(root / "summary.json", {"kind": "test_summary"})
            base = {
                "kind": "problem2_stage2_fixed_partition_mapping_group",
                "case": "case_test",
                "cores": 2,
                "partition_family": "b0",
                "candidates": [{"name": "original"}, {
                    "name": "map_locality",
                    "evaluation_key": "same-key",
                    "cache_hit": False,
                    "plan_hash": "plan-a",
                    "official_result_json_sha256": "result-a",
                }],
            }
            _write(root / "groups" / "case_test" / "k2" / "b0.json", base)
            second = json.loads(json.dumps(base))
            second["partition_family"] = "b1"
            second["candidates"][1]["cache_hit"] = True
            _write(root / "groups" / "case_test" / "k2" / "b1.json", second)
            found = snapshot(root)
            self.assertEqual(found["source_group_count"], 2)
            self.assertEqual(found["evaluation_key_references"], 2)
            self.assertEqual(found["unique_evaluation_keys"], 1)
            self.assertEqual(found["cache_hit_references"], 1)
            self.assertEqual(found["source_cache_path_recorded"], "/external/cache")
            self.assertEqual(len(found["entries"][0]["sources"]), 2)

    def test_snapshot_rejects_inconsistent_run_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(root / "run_identity.json", {
                "cache": "/external/cache",
                "run_identity_sha256": "tampered",
            })
            _write(root / "summary.json", {"kind": "test_summary"})
            with self.assertRaisesRegex(ValueError, "inconsistent"):
                snapshot(root)


if __name__ == "__main__":
    unittest.main()

