"""Run the locked 12-case Problem-1 development set through AUTO."""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from candidate_manager_problem1 import CANDIDATE_PRIORITY, run_candidate_manager
from contest_io import _read_json
from stub_multicore_cut_and_schedule import derive_multicore_plan


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _best_record(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    priority = {name: index for index, name in enumerate(CANDIDATE_PRIORITY)}
    valid = [record for record in records if record["status"] == "evaluated"]
    if not valid:
        raise RuntimeError("no evaluated candidate in manifest")
    return min(valid, key=lambda record: (
        record["makespan"], record["added_copy_bytes"],
        priority[record["name"]]))


def _improvement(best: int, baseline: int | None) -> float | None:
    if baseline is None or baseline == 0:
        return None
    return (baseline - best) / baseline


def run_development_set(
    devset_path: Path,
    data_dir: Path,
    output_root: Path,
    cache_dir: Path,
    config_path: Path,
) -> Dict[str, Any]:
    started = time.perf_counter()
    devset = _read_json(devset_path)
    case_ids = [entry["case"] for entry in devset["cases"]]
    combinations = [(case, cores) for case in case_ids for cores in (2, 3, 4, 5)]
    rows: List[Dict[str, Any]] = []
    method_stats = {
        name: {"winner_count": 0, "valid_comparisons": 0,
               "identical_to_winner_metrics": 0}
        for name in CANDIDATE_PRIORITY
    }
    leave_out_names = ("b1", "b2a_w4", "b2a_w8", "b2a_w16")
    leave_out = {
        name: {
            "combinations": 0,
            "score_changed": 0,
            "makespan_changed": 0,
            "added_copy_bytes_changed": 0,
            "total_makespan_increase": 0,
            "total_added_copy_bytes_change": 0,
        }
        for name in leave_out_names
    }

    for index, (case, cores) in enumerate(combinations, start=1):
        combo_started = time.perf_counter()
        result = run_candidate_manager(
            data_dir / (case + ".json"), cores,
            config_path=config_path,
            output_root=output_root,
            cache_dir=cache_dir,
        )
        manifest = result.manifest
        graph = _read_json(data_dir / (case + ".json"))
        derive_multicore_plan(graph, result.final_plan)
        records = manifest["candidates"]
        by_name = {record["name"]: record for record in records}
        winner = _best_record(records)
        winner_score = (winner["makespan"], winner["added_copy_bytes"])
        if winner["name"] != result.winner_name:
            raise RuntimeError("winner mismatch in {} k{}".format(case, cores))

        for record in records:
            stats = method_stats[record["name"]]
            if record["status"] == "evaluated":
                stats["valid_comparisons"] += 1
                if (record["makespan"], record["added_copy_bytes"]) == winner_score:
                    stats["identical_to_winner_metrics"] += 1
            if record["name"] == winner["name"]:
                stats["winner_count"] += 1

        leave_out_winners: Dict[str, Dict[str, Any]] = {}
        for omitted in leave_out_names:
            alternative = _best_record([
                record for record in records if record["name"] != omitted
            ])
            alternative_score = (
                alternative["makespan"], alternative["added_copy_bytes"])
            stats = leave_out[omitted]
            stats["combinations"] += 1
            if alternative_score != winner_score:
                stats["score_changed"] += 1
            if alternative["makespan"] != winner["makespan"]:
                stats["makespan_changed"] += 1
            if alternative["added_copy_bytes"] != winner["added_copy_bytes"]:
                stats["added_copy_bytes_changed"] += 1
            stats["total_makespan_increase"] += (
                alternative["makespan"] - winner["makespan"])
            stats["total_added_copy_bytes_change"] += (
                alternative["added_copy_bytes"] - winner["added_copy_bytes"])
            leave_out_winners[omitted] = {
                "winner": alternative["name"],
                "makespan": alternative["makespan"],
                "added_copy_bytes": alternative["added_copy_bytes"],
            }

        single = by_name["single"]
        b0 = by_name["b0"]
        row = {
            "case": case,
            "cores": cores,
            "winner": winner["name"],
            "unique_candidates": manifest["unique_plans"],
            "evaluations": manifest["official_evaluations"],
            "cache_hits": manifest["cache_hits"],
            "winner_makespan": winner["makespan"],
            "winner_added_bytes": winner["added_copy_bytes"],
            "single_makespan": single["makespan"],
            "single_added_bytes": single["added_copy_bytes"],
            "b0_makespan": b0["makespan"],
            "b0_added_bytes": b0["added_copy_bytes"],
            "improvement_vs_single": _improvement(
                winner["makespan"], single["makespan"]),
            "improvement_vs_b0": _improvement(
                winner["makespan"], b0["makespan"]),
            "total_wall_time": manifest["timing"]["total_wall_time"],
            "evaluation_time": manifest["timing"]["evaluation_time"],
            "max_memory_bytes": winner["max_memory_bytes"],
            "failed_candidates": ";".join(manifest["failed_candidates"]),
            "leave_one_out": leave_out_winners,
        }
        rows.append(row)
        print(
            "[{}/{}] {} k{} winner={} score=({}, {}) unique={} eval={} "
            "cache={} wall={:.3f}s".format(
                index, len(combinations), case, cores, row["winner"],
                row["winner_makespan"], row["winner_added_bytes"],
                row["unique_candidates"], row["evaluations"],
                row["cache_hits"], time.perf_counter() - combo_started),
            flush=True,
        )

    winner_counts = Counter(row["winner"] for row in rows)
    total_wall = time.perf_counter() - started
    summary = {
        "schema_version": 1,
        "devset_path": str(Path(devset_path).resolve()),
        "case_count": len(case_ids),
        "combination_count": len(rows),
        "cores": [2, 3, 4, 5],
        "results": rows,
        "method_statistics": method_stats,
        "winner_counts": dict(winner_counts),
        "leave_one_candidate_out": leave_out,
        "aggregate": {
            "official_evaluations": sum(row["evaluations"] for row in rows),
            "cache_hits": sum(row["cache_hits"] for row in rows),
            "candidate_manager_wall_time": sum(
                row["total_wall_time"] for row in rows),
            "evaluation_time": sum(row["evaluation_time"] for row in rows),
            "experiment_wall_time": total_wall,
            "failed_candidate_count": sum(
                bool(row["failed_candidates"]) for row in rows),
        },
    }
    artifacts = Path(devset_path).resolve().parent
    json_path = artifacts / "problem1_development_summary.json"
    csv_path = artifacts / "problem1_development_summary.csv"
    _write_json(json_path, summary)
    fields = [
        "case", "cores", "winner", "unique_candidates", "evaluations",
        "cache_hits", "winner_makespan", "winner_added_bytes",
        "single_makespan", "single_added_bytes", "b0_makespan",
        "b0_added_bytes", "improvement_vs_single", "improvement_vs_b0",
        "total_wall_time", "evaluation_time", "max_memory_bytes",
        "failed_candidates",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in fields})
    print("summary: {}".format(json_path), flush=True)
    return summary


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="运行 Problem 1 固定 12-case 开发集 AUTO 实验")
    parser.add_argument(
        "--devset", default="artifacts/problem1_devset.json")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument(
        "--output-root", default="artifacts/problem1_candidates")
    parser.add_argument(
        "--cache-dir", default="artifacts/problem1_candidate_cache")
    parser.add_argument("--config", default="data/config.txt")
    args = parser.parse_args(argv)
    run_development_set(
        Path(args.devset), Path(args.data_dir), Path(args.output_root),
        Path(args.cache_dir), Path(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
