"""Black-box checks of the supplied Problem-3 evaluator's cache semantics."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from multicore_cut_evaluate_problem_2 import evaluate_scene_b
from multicore_cut_evaluate_problem_3 import evaluate_problem_3


CAPACITY = {"L1": 524288, "UB": 131072}


def graph_and_plan(targets, dummy_sizes=()):
    """Independent legal subgraphs; optional oversize reads stagger issue times."""
    tensors = [
        {"id": tid, "pos": "DDR", "size": 64}
        for tid in sorted(set(targets))
    ]
    ops, edges, mapping, schedules = [], [], {}, []
    for core, tid in enumerate(targets):
        op_id = 100 + core
        ops.append({"id": op_id, "op": "ADD", "pipe": "PIPE_V", "cycles": 1})
        edges.append({"source": tid, "target": op_id})
        if core < len(dummy_sizes) and dummy_sizes[core]:
            dummy_id = core + 1
            tensors.append({
                "id": dummy_id, "pos": "DDR", "size": dummy_sizes[core],
            })
            edges.append({"source": dummy_id, "target": op_id})
        mapping[str(op_id)] = core
        schedules.append([core])
    return (
        {"ops": ops, "tensors": tensors, "edges": edges},
        {"node_to_subgraph": mapping, "core_schedules": schedules},
    )


def evaluate(graph, plan, cache_capacity=128):
    return evaluate_problem_3(
        graph, plan, 60, CAPACITY, 500, cache_capacity, 250,
    )


class Problem3SemanticsTests(unittest.TestCase):
    def test_legal_plan_and_simultaneous_reads_both_miss(self):
        graph, plan = graph_and_plan([10, 10])
        result = evaluate(graph, plan)
        reads = [
            e for e in result["cache_events"]
            if e["event"] in ("hit", "miss")
        ]
        self.assertEqual([(e["time"], e["event"]) for e in reads],
                         [(0, "miss"), (0, "miss")])
        self.assertEqual(result["cache_stats"]["copy_in_misses"], 2)
        self.assertEqual(result["cache_stats"]["copy_in_hits"], 0)
        self.assertEqual(
            [e["tensor_id"] for e in result["cache_final_entries"]], [10],
        )
        with self.assertRaises(Exception):
            evaluate(graph, {"node_to_subgraph": {}, "core_schedules": [[]]})

    def test_tensor_larger_than_cache_never_inserted(self):
        graph, plan = graph_and_plan([10, 10])
        graph["tensors"][0]["size"] = 256
        result = evaluate(graph, plan, cache_capacity=128)
        self.assertEqual(result["cache_stats"]["copy_in_misses"], 2)
        self.assertEqual(result["cache_final_entries"], [])
        self.assertFalse(any(e["event"] == "insert"
                             for e in result["cache_events"]))

    def test_fifo_hit_does_not_refresh_and_byte_weighted_metrics(self):
        # Issue order: A, oversize dummy+B, dummy+A, dummy+C, dummy+A.
        # Each dummy misses without entering the 128-byte cache.
        graph, plan = graph_and_plan(
            [10, 20, 10, 30, 10], [0, 300, 600, 900, 1200],
        )
        result = evaluate(graph, plan)
        relevant = [e for e in result["cache_events"]
                    if e["tensor_id"] in (10, 20, 30)]
        reads = [(e["tensor_id"], e["event"])
                 for e in relevant if e["event"] in ("hit", "miss")]
        self.assertEqual(reads, [
            (10, "miss"), (20, "miss"), (10, "hit"),
            (30, "miss"), (10, "miss"),
        ])
        insertion_c = next(e for e in relevant
                           if e["event"] == "insert" and e["tensor_id"] == 30)
        self.assertEqual(insertion_c["evicted_tensor_ids"], [10])
        stats = result["cache_stats"]
        # Five 64-byte target reads plus 300+600+900+1200 dummy bytes.
        # Exactly one 64-byte target read is an L2 hit.
        self.assertEqual(stats["hit_bytes"], 64)
        self.assertEqual(stats["miss_bytes"], 4 * 64 + 300 + 600 + 900 + 1200)
        self.assertAlmostEqual(stats["hit_rate"], 64 / 3320)
        self.assertAlmostEqual(
            stats["hit_rate"],
            stats["hit_bytes"] / (stats["hit_bytes"] + stats["miss_bytes"]),
        )
        self.assertNotAlmostEqual(
            stats["hit_rate"], stats["hits"] / stats["accesses"],
        )
        without_cache = evaluate_scene_b(graph, plan, 60, CAPACITY, 500)
        self.assertEqual(result["data_movement_bytes"],
                         without_cache["data_movement_bytes"])
        self.assertGreater(result["data_movement_bytes"]["added_copy_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
