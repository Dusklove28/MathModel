"""Extract concrete timeline evidence for one G1 paired case-core example."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from audit_problem3_assets import sha256_file
from run_problem3_g1 import REPO, load_json, write_json


def overlap_summary(intervals: list[tuple[int, int]]) -> dict:
    events = []
    for start, end in intervals:
        if end > start:
            events.append((start, 1))
            events.append((end, -1))
    events.sort(key=lambda item: (item[0], item[1]))
    active = maximum = occupied = overlapping = 0
    previous = None
    for now, delta in events:
        if previous is not None and now > previous:
            span = now - previous
            if active:
                occupied += span
            if active > 1:
                overlapping += span
        active += delta
        maximum = max(maximum, active)
        previous = now
    return {
        "max_simultaneous": maximum,
        "cycles_with_any": occupied,
        "cycles_with_multiple": overlapping,
    }


def timeline_summary(result: dict) -> dict:
    paths = defaultdict(list)
    pipe_counts = Counter()
    pipe_durations = Counter()
    for core in result.get("per_core_timeline", []):
        for op in core.get("ops", []):
            if op["op"] in ("COPY_IN", "COPY_OUT"):
                paths[op.get("memory_path", "unknown")].append(
                    (op["start"], op["end"]))
            pipe_counts[op["pipe"]] += 1
            pipe_durations[op["pipe"]] += op["duration"]
    summary = {
        "makespan": result["makespan"],
        "copy_path_op_counts": {path: len(intervals)
                                for path, intervals in paths.items()},
        "copy_path_overlap": {path: overlap_summary(intervals)
                              for path, intervals in paths.items()},
        "pipe_op_counts": dict(pipe_counts),
        "pipe_sum_durations": dict(pipe_durations),
        "cross_core_transfer_count": len(result.get("cross_core_transfers", [])),
        "cross_core_release_delay_cycles": result["cross_core_copy_delay_cycles"],
        "cache_stats": result.get("cache_stats"),
    }
    if "cache_events" in result:
        cache_events = result["cache_events"]
        first_insert = {}
        pending_miss_by_tensor = defaultdict(list)
        duplicate_before_fill = 0
        for event in cache_events:
            tid = event["tensor_id"]
            if event["event"] == "miss":
                if tid not in first_insert and pending_miss_by_tensor[tid]:
                    duplicate_before_fill += 1
                pending_miss_by_tensor[tid].append(event["time"])
            elif event["event"] == "insert":
                first_insert.setdefault(tid, event["time"])
        insertions = [e for e in cache_events if e["event"] == "insert"]
        hits = [e for e in cache_events if e["event"] == "hit"]
        misses = [e for e in cache_events if e["event"] == "miss"]
        summary["cache_event_evidence"] = {
            "first_miss": misses[0] if misses else None,
            "first_completion_fill": insertions[0] if insertions else None,
            "first_hit": hits[0] if hits else None,
            "hit_count": len(hits),
            "miss_count": len(misses),
            "insertion_count": len(insertions),
            "eviction_count": sum(len(e["evicted_tensor_ids"])
                                  for e in insertions),
            "simultaneous_or_early_repeat_misses_before_first_fill":
                duplicate_before_fill,
            "first_eviction": next((e for e in insertions
                                    if e["evicted_tensor_ids"]), None),
        }
    return summary


def build(g1: Path, case: str, cores: int) -> dict:
    group_dir = g1 / "groups" / case / f"k{cores}"
    manifest = load_json(group_dir / "group.json")
    no_l2_path = group_dir / "no_l2_official.json"
    l2_path = group_dir / "l2_official.json"
    if sha256_file(no_l2_path) != manifest["no_l2_result_file_sha256"]:
        raise ValueError("no-L2 official result SHA256 mismatch")
    if sha256_file(l2_path) != manifest["l2_result_file_sha256"]:
        raise ValueError("L2 official result SHA256 mismatch")
    no_l2, l2 = load_json(no_l2_path), load_json(l2_path)
    return {
        "case": case, "cores": cores,
        "plan_file_sha256": manifest["plan_file_sha256"],
        "no_l2_result_file_sha256": manifest["no_l2_result_file_sha256"],
        "l2_result_file_sha256": manifest["l2_result_file_sha256"],
        "same_core_speedup": no_l2["makespan"] / l2["makespan"],
        "nominal_data_movement_equal": (
            no_l2["data_movement_bytes"] == l2["data_movement_bytes"]),
        "no_l2": timeline_summary(no_l2),
        "l2": timeline_summary(l2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case")
    parser.add_argument("cores", type=int)
    parser.add_argument("--g1", type=Path,
                        default=REPO / "artifacts" / "problem3_g1_paired")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build(args.g1, args.case, args.cores)
    output = args.output or (REPO / "artifacts" / "problem3_timeline_evidence" /
                             f"{args.case}_k{args.cores}.json")
    write_json(output, report)
    print(f"{args.case}/k{args.cores} "
          f"noL2={report['no_l2']['makespan']} "
          f"L2={report['l2']['makespan']} "
          f"hits={report['l2']['cache_event_evidence']['hit_count']} "
          f"evictions={report['l2']['cache_event_evidence']['eviction_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
