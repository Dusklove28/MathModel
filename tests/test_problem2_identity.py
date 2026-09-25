import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from candidate_manager_problem2 import json_sha256
from problem2_identity import plan_json_sha256


class Problem2IdentityTests(unittest.TestCase):
    def test_plan_hash_is_stable_across_json_key_types(self):
        in_memory = {
            "node_to_subgraph": {1: 0, 2: 1, 10: 2},
            "core_schedules": [[0, 2], [1]],
        }
        from_disk = {
            "node_to_subgraph": {"1": 0, "2": 1, "10": 2},
            "core_schedules": [[0, 2], [1]],
        }
        self.assertNotEqual(json_sha256(in_memory), json_sha256(from_disk))
        self.assertEqual(
            plan_json_sha256(in_memory), plan_json_sha256(from_disk))

    def test_normalization_rejects_colliding_keys(self):
        with self.assertRaisesRegex(Exception, "collide"):
            plan_json_sha256({
                "node_to_subgraph": {1: 0, "1": 1},
                "core_schedules": [[0], [1]],
            })


if __name__ == "__main__":
    unittest.main()

