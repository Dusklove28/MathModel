"""Deterministically select a 12-case Problem-1 development set.

Selection uses graph structure only.  It never reads solver or evaluator
results, preventing performance-based case cherry-picking.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from solver_problem1 import GraphInfo
from contest_io import _read_json


PRIMARY_FEATURES = (
    "eligible_ops",
    "num_levels",
    "mean_level_width",
    "dependency_edges",
    "tensor_bytes",
)


def graph_static_features(path: Path) -> Dict[str, Any]:
    graph_json = _read_json(path)
    graph = GraphInfo.from_graph(graph_json)
    level_widths = [len(nodes) for nodes in graph.nodes_by_level.values()]
    num_levels = len(level_widths)
    return {
        "case": path.stem,
        "eligible_ops": len(graph.eligible_ops),
        "num_levels": num_levels,
        "mean_level_width": (
            len(graph.eligible_ops) / num_levels if num_levels else 0.0),
        "max_level_width": max(level_widths, default=0),
        "dependency_edges": sum(len(graph.succs[node])
                                for node in graph.eligible_ops),
        "tensor_bytes": sum(graph.tensor_size.values()),
        "tensor_count": len(graph.tensor_size),
    }


def _normalized_vectors(
    rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Tuple[float, ...]]:
    transformed = {
        row["case"]: tuple(math.log1p(float(row[name]))
                           for name in PRIMARY_FEATURES)
        for row in rows
    }
    minima = [min(vector[index] for vector in transformed.values())
              for index in range(len(PRIMARY_FEATURES))]
    maxima = [max(vector[index] for vector in transformed.values())
              for index in range(len(PRIMARY_FEATURES))]
    return {
        case: tuple(
            0.0 if maxima[index] == minima[index] else
            (value - minima[index]) / (maxima[index] - minima[index])
            for index, value in enumerate(vector)
        )
        for case, vector in transformed.items()
    }


def _distance(left: Sequence[float], right: Sequence[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def select_development_cases(
    rows: Sequence[Mapping[str, Any]],
    count: int = 12,
) -> Tuple[List[str], Dict[str, List[str]], Dict[str, Tuple[float, ...]]]:
    """Cover static extremes, the center, then maximize minimum distance."""

    if count < 1 or count > len(rows):
        raise ValueError("count must be between 1 and the number of cases")
    by_case = {str(row["case"]): row for row in rows}
    case_ids = sorted(by_case)
    vectors = _normalized_vectors(rows)
    selected: List[str] = []
    reasons: Dict[str, List[str]] = {}

    def add(case: str, reason: str) -> None:
        reasons.setdefault(case, []).append(reason)
        if case not in selected and len(selected) < count:
            selected.append(case)

    for feature in PRIMARY_FEATURES:
        minimum = min(case_ids, key=lambda case: (
            float(by_case[case][feature]), case))
        maximum = min(case_ids, key=lambda case: (
            -float(by_case[case][feature]), case))
        add(minimum, "minimum {}".format(feature))
        add(maximum, "maximum {}".format(feature))

    centroid = tuple(
        sum(vectors[case][index] for case in case_ids) / len(case_ids)
        for index in range(len(PRIMARY_FEATURES))
    )
    central = min(case_ids, key=lambda case: (
        _distance(vectors[case], centroid), case))
    add(central, "nearest normalized feature centroid")

    while len(selected) < count:
        remaining = [case for case in case_ids if case not in selected]
        next_case = min(remaining, key=lambda case: (
            -min(_distance(vectors[case], vectors[chosen])
                 for chosen in selected),
            case,
        ))
        add(next_case, "farthest-point structural diversity fill")
    return selected, reasons, vectors


def build_devset(data_dir: Path, count: int = 12) -> Dict[str, Any]:
    paths = sorted(Path(data_dir).glob("case_*.json"))
    if not paths:
        raise FileNotFoundError("no case_*.json files found in {}".format(data_dir))
    rows = [graph_static_features(path) for path in paths]
    selected, reasons, vectors = select_development_cases(rows, count=count)
    by_case = {row["case"]: row for row in rows}
    return {
        "schema_version": 1,
        "selection_locked": True,
        "selection_uses_performance_results": False,
        "source_case_count": len(rows),
        "selected_count": len(selected),
        "primary_features": list(PRIMARY_FEATURES),
        "normalization": "log1p then per-feature min-max over all source cases",
        "selection_rule": (
            "deterministic min/max anchors for every primary feature, nearest "
            "normalized centroid, then farthest-point fill; case id breaks ties"
        ),
        "cases": [
            {
                **dict(by_case[case]),
                "selection_reasons": reasons[case],
                "normalized_primary_features": list(vectors[case]),
            }
            for case in selected
        ],
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="静态、确定性选择 Problem 1 十二案例开发集")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--output", default="artifacts/problem1_devset.json")
    args = parser.parse_args(argv)
    result = build_devset(Path(args.data_dir), count=args.count)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print("selected: {}".format(
        ", ".join(case["case"] for case in result["cases"])))
    print("output: {}".format(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
