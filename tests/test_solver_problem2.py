import copy
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from solver_problem1 import GraphInfo
from evaluation_validation import validate_task_order
from solver_problem2 import (
    MAPPING_POLICIES,
    build_mapping_features,
    generate_scene_b_mapping_candidates,
    scene_b_mapping_proxy,
)
from stub_multicore_cut_and_schedule import derive_multicore_plan


def _io_chain_graph():
    return {
        "ops": [
            {"id": 10, "op": "COPY_IN", "pipe": "PIPE_MTE2", "cycles": 0},
            {"id": 1, "op": "ADD", "pipe": "PIPE_V", "cycles": 10},
            {"id": 2, "op": "MUL", "pipe": "PIPE_M", "cycles": 20},
            {"id": 11, "op": "COPY_OUT", "pipe": "PIPE_MTE3", "cycles": 0},
        ],
        "tensors": [
            {"id": 100, "pos": "DDR", "size": 32},
            {"id": 101, "pos": "UB", "size": 32},
            {"id": 102, "pos": "UB", "size": 64},
            {"id": 103, "pos": "UB", "size": 16},
            {"id": 104, "pos": "DDR", "size": 16},
        ],
        "edges": [
            {"source": 100, "target": 10},
            {"source": 10, "target": 101},
            {"source": 101, "target": 1},
            {"source": 1, "target": 102},
            {"source": 102, "target": 2},
            {"source": 1, "target": 2, "data_size": 7},
            {"source": 2, "target": 103},
            {"source": 103, "target": 11},
            {"source": 11, "target": 104},
        ],
    }


def _base_plan():
    return {
        "node_to_subgraph": {1: 0, 2: 1},
        "core_schedules": [[0], [1]],
    }


class Problem2MappingSolverTests(unittest.TestCase):
    def test_proxy_matches_scene_b_transfer_counting_rules(self):
        graph_json = _io_chain_graph()
        graph = GraphInfo.from_graph(graph_json)
        features = build_mapping_features(graph_json, graph, _base_plan())
        proxy = scene_b_mapping_proxy(
            graph,
            features,
            {0: 0, 1: 1},
            2,
            bandwidth=60,
            capacity={"L1": 524288, "UB": 131072},
            cross_core_delay=500,
        )
        self.assertEqual(proxy["input_read_bytes"], 32)
        self.assertEqual(proxy["output_write_bytes"], 16)
        self.assertEqual(proxy["cross_tensor_payload_bytes"], 64)
        self.assertEqual(proxy["direct_cross_payload_bytes"], 7)
        self.assertEqual(proxy["cross_transfer_count"], 2)
        self.assertEqual(proxy["scheduled_copy_bytes_proxy"], 190)

    def test_same_core_eliminates_cross_payload_but_not_graph_io(self):
        graph_json = _io_chain_graph()
        graph = GraphInfo.from_graph(graph_json)
        features = build_mapping_features(graph_json, graph, _base_plan())
        proxy = scene_b_mapping_proxy(graph, features, {0: 0, 1: 0}, 2)
        self.assertEqual(proxy["cross_payload_bytes"], 0)
        self.assertEqual(proxy["cross_transfer_count"], 0)
        self.assertEqual(proxy["scheduled_copy_bytes_proxy"], 48)

    def test_candidates_are_deterministic_legal_and_keep_partition(self):
        graph = _io_chain_graph()
        base = _base_plan()
        first = generate_scene_b_mapping_candidates(
            graph, base, max_moves=1, search_width=2)
        second = generate_scene_b_mapping_candidates(
            graph, base, max_moves=1, search_width=2)
        self.assertEqual(first, second)
        self.assertEqual(tuple(first), MAPPING_POLICIES)
        for plan, diagnostics in first.values():
            self.assertEqual(plan["node_to_subgraph"], base["node_to_subgraph"])
            self.assertEqual(set(plan), {"node_to_subgraph", "core_schedules"})
            view = derive_multicore_plan(graph, plan)
            validate_task_order(view)
            self.assertTrue(diagnostics["fixed_partition"])

    def test_input_plan_is_not_mutated(self):
        graph = _io_chain_graph()
        base = _base_plan()
        before = copy.deepcopy(base)
        generate_scene_b_mapping_candidates(
            graph, base, max_moves=1, search_width=2)
        self.assertEqual(base, before)

    def test_single_policy_generation_is_explicit_and_deterministic(self):
        generated = generate_scene_b_mapping_candidates(
            _io_chain_graph(), _base_plan(), max_moves=1, search_width=2,
            policies=("locality",),
        )
        self.assertEqual(tuple(generated), ("locality",))
        self.assertEqual(generated["locality"][1]["policy"], "locality")


if __name__ == "__main__":
    unittest.main()

