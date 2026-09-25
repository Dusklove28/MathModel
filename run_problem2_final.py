#!/usr/bin/env python3
"""Run the frozen final Problem-2 portfolio with strict resume support.

The run is parallel across cases and sequential from one to five cores within
each case, which permits the previous final winner to be inherited by appending
one empty core.  Candidate generation remains deterministic; final selection
uses only the unmodified official Scene-B evaluator.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from candidate_manager_problem1 import sha256_file
from candidate_manager_problem2 import json_sha256, official_scene_b_hash
from candidate_manager_problem2_final import (
    IMPLEMENTATION_VERSION,
    final_implementation_hash,
    reusable_final_group,
    run_final_case,
)
from candidate_manager_problem2_stage2 import (
    MAPPING_FAMILIES,
    load_verified_stage1_selected_references,
    stage2_implementation_hash,
)
from contest_io import _read_json


REPO = Path(__file__).resolve().parent
SMOKE_CASES = ("case_001", "case_050", "case_100")
SPLIT_LIST_FIELDS = (
    "development_cases", "validation_cases", "holdout_cases")
EXPECTED_SPLIT_LIST_HASH = (
    "09f131d078750d9bf8dd5db71835850195d9230924406918e93204c1daa7daaa")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    empty_fields: Sequence[str] = (),
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: List[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    if not fields:
        fields = list(empty_fields)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _append_history(output: Path, record: Mapping[str, Any]) -> None:
    path = output / "invocation_history.json"
    if path.is_file():
        try:
            history = _read_json(path)
        except Exception:
            history = None
    else:
        history = None
    if not isinstance(history, dict) or not isinstance(
            history.get("invocations"), list):
        history = {
            "schema_version": 1,
            "kind": "problem2_final_invocation_history",
            "invocations": [],
        }
    history["invocations"].append(dict(record))
    history["latest_invocation_id"] = record["invocation_id"]
    history["updated_at"] = _utc_now()
    _write_json(path, history)


def _load_split(path: Path) -> Dict[str, Any]:
    split = _read_json(path)
    lists = {key: [str(case) for case in split.get(key, [])]
             for key in SPLIT_LIST_FIELDS}
    if tuple(len(lists[key]) for key in SPLIT_LIST_FIELDS) != (12, 18, 70):
        raise ValueError("Problem-2 split must contain 12/18/70 cases")
    all_cases = sum((lists[key] for key in SPLIT_LIST_FIELDS), [])
    if len(all_cases) != 100 or len(set(all_cases)) != 100:
        raise ValueError("Problem-2 split must cover 100 unique cases")
    payload = {key: lists[key] for key in SPLIT_LIST_FIELDS}
    found_hash = json_sha256(payload)
    if (found_hash != EXPECTED_SPLIT_LIST_HASH
            or split.get("case_lists_canonical_sha256") != found_hash):
        raise ValueError("Problem-2 split case-list hash mismatch")
    return split


def _phase_cases(
    split: Mapping[str, Any], phase: str,
    custom_cases: Sequence[str] | None,
) -> Tuple[str, ...]:
    if phase == "smoke":
        return SMOKE_CASES
    if phase == "all":
        return tuple(sorted(
            case for key in SPLIT_LIST_FIELDS for case in split[key]))
    if phase == "custom":
        if not custom_cases:
            raise ValueError("--cases is required for --phase custom")
        return tuple(custom_cases)
    field = {
        "development": "development_cases",
        "validation": "validation_cases",
        "holdout": "holdout_cases",
    }[phase]
    return tuple(str(case) for case in split[field])


def _discover_cases(repo: Path) -> Tuple[str, ...]:
    return tuple(path.stem for path in sorted((repo / "data").glob(
        "case_[0-9][0-9][0-9].json")))


def _build_run_identity(
    *, repo: Path, baseline: Path, split_path: Path,
) -> Tuple[Dict[str, Any], str]:
    all_cases = _discover_cases(repo)
    if len(all_cases) != 100:
        raise ValueError("expected exactly 100 official case files")
    all_pairs = [(case, cores) for case in all_cases for cores in range(1, 6)]
    selected = load_verified_stage1_selected_references(
        graph_root=repo / "data",
        config_path=repo / "data" / "config.txt",
        baseline_root=baseline,
        pairs=all_pairs,
    )
    implementation_hash, implementation_files = final_implementation_hash(
        Path(__file__).resolve())
    stage2_hash, stage2_files = stage2_implementation_hash()
    evaluator_hash, evaluator_files = official_scene_b_hash()
    identity = {
        "schema_version": 1,
        "kind": "problem2_final_portfolio_identity",
        "implementation_version": IMPLEMENTATION_VERSION,
        "implementation_sha256": implementation_hash,
        "implementation_file_sha256": implementation_files,
        "stage2_implementation_sha256": stage2_hash,
        "stage2_implementation_file_sha256": stage2_files,
        "official_evaluator_sha256": evaluator_hash,
        "official_evaluator_file_sha256": evaluator_files,
        "config_sha256": sha256_file(repo / "data" / "config.txt"),
        "graph_file_sha256": {
            case: sha256_file(repo / "data" / (case + ".json"))
            for case in all_cases
        },
        "graph_set_sha256": json_sha256({
            case: sha256_file(repo / "data" / (case + ".json"))
            for case in all_cases
        }),
        "split_file_sha256": sha256_file(split_path),
        "split_case_lists_sha256": EXPECTED_SPLIT_LIST_HASH,
        "stage1_selected_results_file_sha256": (
            selected.selected_results_file_hash),
        "stage1_selected_reference_sha256": selected.selected_reference_hash,
        "candidate_contract": {
            "stage1_actual_winner": True,
            "literal_single": True,
            "mapping_families": list(MAPPING_FAMILIES),
            "original_mapping_per_family": True,
            "mapping_policy": "locality",
            "mapping_max_moves": 2,
            "mapping_search_width": 12,
            "recursive_previous_final_winner_inheritance": True,
            "minimum_logical_candidates_per_multicore_cell": 12,
            "selection": ["makespan_cycles", "added_copy_bytes"],
        },
    }
    digest = json_sha256(identity)
    identity["run_identity_sha256"] = digest
    return identity, implementation_hash


def _run_case_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        groups, failures = run_final_case(
            graph_path=Path(payload["graph_path"]),
            config_path=Path(payload["config_path"]),
            baseline_root=Path(payload["baseline"]),
            output_root=Path(payload["output"]),
            cache_dir=Path(payload["cache"]),
            max_cores=int(payload["max_cores"]),
            max_moves=int(payload["max_moves"]),
            search_width=int(payload["search_width"]),
            run_identity_sha256=str(payload["run_identity_sha256"]),
            implementation_hash=str(payload["implementation_hash"]),
        )
        return {"groups": groups, "failures": failures, "fatal": None}
    except Exception as error:
        return {
            "groups": [],
            "failures": [],
            "fatal": {
                "case": Path(payload["graph_path"]).stem,
                "cores": "*",
                "candidate": "*case_infrastructure*",
                "error_type": type(error).__name__,
                "error": "{}: {}".format(type(error).__name__, error),
            },
        }


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return (ordered[lower] * (upper - position)
            + ordered[upper] * (position - lower))


def _distribution(values: Sequence[float], prefix: str) -> Dict[str, Any]:
    clean = [float(value) for value in values]
    if not clean:
        return {
            prefix + "_count": 0,
            prefix + "_mean": None,
            prefix + "_median": None,
            prefix + "_p05": None,
            prefix + "_p95": None,
            prefix + "_min": None,
            prefix + "_max": None,
        }
    return {
        prefix + "_count": len(clean),
        prefix + "_mean": statistics.fmean(clean),
        prefix + "_median": statistics.median(clean),
        prefix + "_p05": _quantile(clean, 0.05),
        prefix + "_p95": _quantile(clean, 0.95),
        prefix + "_min": min(clean),
        prefix + "_max": max(clean),
    }


def _two_sided_sign_test(improved: int, worsened: int) -> float | None:
    non_ties = improved + worsened
    if non_ties == 0:
        return None
    smaller = min(improved, worsened)
    lower_tail = sum(math.comb(non_ties, index)
                     for index in range(smaller + 1)) / (2 ** non_ties)
    return min(1.0, 2.0 * lower_tail)


def _load_all_reusable_groups(
    repo: Path, output: Path, run_identity_sha256: str,
) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for case in _discover_cases(repo):
        for cores in range(1, 6):
            group = reusable_final_group(
                group_path=output / "groups" / case / "k{}.json".format(cores),
                run_identity_sha256=run_identity_sha256,
                case=case,
                cores=cores,
            )
            if group is not None:
                groups.append(group)
    return groups


def _candidate_rows(groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in sorted(groups, key=lambda item: (item["case"], item["cores"])):
        for candidate in group.get("candidates", []):
            metrics = candidate.get("metrics") or {}
            timing = candidate.get("timing") or {}
            rows.append({
                "case": group["case"],
                "cores": group["cores"],
                "candidate": candidate.get("name"),
                "family": candidate.get("family"),
                "status": candidate.get("status"),
                "legal": candidate.get("legal"),
                "deduplicated": candidate.get("deduplicated"),
                "canonical_candidate": candidate.get("canonical_candidate"),
                "cache_hit": candidate.get("cache_hit"),
                "failure_stage": candidate.get("failure_stage"),
                "error_type": candidate.get("error_type"),
                "error": candidate.get("error"),
                "makespan_cycles": metrics.get("makespan_cycles"),
                "added_copy_bytes": metrics.get("added_copy_bytes"),
                "partition_added_copy_bytes": metrics.get(
                    "partition_added_copy_bytes"),
                "spill_added_copy_bytes": metrics.get("spill_added_copy_bytes"),
                "cross_task_traffic_bytes": metrics.get(
                    "cross_task_traffic_bytes"),
                "cross_core_transfer_count": metrics.get(
                    "cross_core_transfer_count"),
                "used_core_count": metrics.get("used_core_count"),
                "per_core_utilization": json.dumps(
                    metrics.get("per_core_utilization"), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")),
                "plan_hash": candidate.get("plan_hash"),
                "canonical_signature": candidate.get("canonical_signature"),
                "evaluation_key": candidate.get("evaluation_key"),
                "official_result_json_sha256": candidate.get(
                    "official_result_json_sha256"),
                "generation_seconds": timing.get("generation_seconds"),
                "validation_seconds": timing.get("validation_seconds"),
                "cache_lookup_seconds": timing.get("cache_lookup_seconds"),
                "official_evaluation_seconds": timing.get(
                    "official_evaluation_seconds"),
                "invocation_wall_seconds": timing.get(
                    "invocation_wall_seconds"),
                "source": json.dumps(candidate.get("source"), ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":")),
            })
    return rows


def _selected_rows(groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in sorted(groups, key=lambda item: (item["case"], item["cores"])):
        winner = group["winner"]
        metrics = winner["metrics"]
        fallback = group["stage1_fallback"]
        delta = group["paired_delta_vs_stage1"]
        rows.append({
            "case": group["case"],
            "cores": group["cores"],
            "status": group["status"],
            "winner": winner["name"],
            "winner_family": winner["family"],
            "makespan_cycles": metrics["makespan_cycles"],
            "added_copy_bytes": metrics["added_copy_bytes"],
            "partition_added_copy_bytes": metrics[
                "partition_added_copy_bytes"],
            "spill_added_copy_bytes": metrics["spill_added_copy_bytes"],
            "cross_task_traffic_bytes": metrics["cross_task_traffic_bytes"],
            "cross_core_transfer_count": metrics[
                "cross_core_transfer_count"],
            "used_core_count": metrics["used_core_count"],
            "per_core_utilization": json.dumps(
                metrics.get("per_core_utilization"), ensure_ascii=False,
                sort_keys=True, separators=(",", ":")),
            "stage1_winner": fallback["winner"],
            "stage1_makespan_cycles": fallback["metrics"]["makespan_cycles"],
            "stage1_added_copy_bytes": fallback["metrics"]["added_copy_bytes"],
            "makespan_delta_vs_stage1": delta["makespan_cycles"],
            "added_copy_delta_vs_stage1": delta["added_copy_bytes"],
            "spill_delta_vs_stage1": delta["spill_added_copy_bytes"],
            "improved_vs_stage1": int(delta["makespan_cycles"]) < 0,
            "candidate_failures": len(group.get("failed_candidates", [])),
            "group_wall_seconds": group["timing"]["group_wall_seconds"],
            "final_plan_hash": group["final_plan_hash"],
            "final_plan_file_sha256": group["final_plan_file_sha256"],
            "final_official_result_json_sha256": group[
                "final_official_result_json_sha256"],
        })
    return rows


def _speedup_and_aggregate(
    selected: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_key = {(str(row["case"]), int(row["cores"])): row for row in selected}
    speedups: List[Dict[str, Any]] = []
    for case in sorted({str(row["case"]) for row in selected}):
        baseline = by_key.get((case, 1))
        if baseline is None:
            continue
        denominator = int(baseline["makespan_cycles"])
        for cores in range(1, 6):
            row = by_key.get((case, cores))
            if row is None:
                continue
            speedups.append({
                "case": case,
                "cores": cores,
                "single_makespan_cycles": denominator,
                "final_makespan_cycles": int(row["makespan_cycles"]),
                "speedup": denominator / int(row["makespan_cycles"]),
            })
    aggregates: List[Dict[str, Any]] = []
    for cores in range(1, 6):
        core_speedups = [float(row["speedup"]) for row in speedups
                         if int(row["cores"]) == cores]
        core_selected = [row for row in selected if int(row["cores"]) == cores]
        if not core_selected:
            continue
        utilization_all: List[float] = []
        utilization_active: List[float] = []
        for row in core_selected:
            utilization = json.loads(str(row["per_core_utilization"]))
            for core_record in utilization:
                fraction = float(core_record["active_any_pipe_fraction"])
                utilization_all.append(fraction)
                if int(core_record["op_count"]) > 0:
                    utilization_active.append(fraction)
        degraded = [row for row in speedups
                    if int(row["cores"]) == cores
                    and float(row["speedup"]) < 1.0]
        aggregate = {
            "cores": cores,
            "case_count": len(core_selected),
            "speedup_case_count": len(core_speedups),
            "mean_speedup": (statistics.fmean(core_speedups)
                             if core_speedups else None),
            "median_speedup": (statistics.median(core_speedups)
                               if core_speedups else None),
            "p05_speedup": _quantile(core_speedups, 0.05),
            "p95_speedup": _quantile(core_speedups, 0.95),
            "worst_speedup": min(core_speedups) if core_speedups else None,
            "best_speedup": max(core_speedups) if core_speedups else None,
            "mean_makespan_cycles": statistics.fmean(
                float(row["makespan_cycles"]) for row in core_selected),
            "median_makespan_cycles": statistics.median(
                float(row["makespan_cycles"]) for row in core_selected),
            "mean_added_copy_bytes": statistics.fmean(
                float(row["added_copy_bytes"]) for row in core_selected),
            "mean_spill_added_copy_bytes": statistics.fmean(
                float(row["spill_added_copy_bytes"]) for row in core_selected),
            "mean_cross_task_traffic_bytes": statistics.fmean(
                float(row["cross_task_traffic_bytes"]) for row in core_selected),
            "mean_cross_core_transfer_count": statistics.fmean(
                float(row["cross_core_transfer_count"])
                for row in core_selected),
            "mean_requested_core_utilization": (
                statistics.fmean(utilization_all) if utilization_all else None),
            "mean_active_core_utilization": (
                statistics.fmean(utilization_active)
                if utilization_active else None),
            "speedup_below_one_count": len(degraded),
            "speedup_below_one_rate": (
                len(degraded) / len(core_speedups) if core_speedups else None),
            "improved_vs_stage1_cells": sum(
                bool(row["improved_vs_stage1"]) for row in core_selected),
            "candidate_failure_count": sum(
                int(row["candidate_failures"]) for row in core_selected),
        }
        aggregate.update(_distribution(
            [float(row["makespan_cycles"]) for row in core_selected],
            "makespan_cycles"))
        aggregate.update(_distribution(
            [float(row["added_copy_bytes"]) for row in core_selected],
            "added_copy_bytes"))
        aggregate.update(_distribution(
            [float(row["spill_added_copy_bytes"]) for row in core_selected],
            "spill_added_copy_bytes"))
        aggregate.update(_distribution(
            [float(row["cross_task_traffic_bytes"]) for row in core_selected],
            "cross_task_traffic_bytes"))
        aggregate.update(_distribution(
            [float(row["cross_core_transfer_count"])
             for row in core_selected],
            "cross_core_transfer_count"))
        aggregates.append(aggregate)
    return speedups, aggregates


def _paired_candidate_summary(
    groups: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    paired: Dict[str, List[Tuple[int, int, int, int]]] = {}
    families: Dict[str, str] = {}
    for group in groups:
        baseline = group["stage1_fallback"]["metrics"]
        baseline_makespan = int(baseline["makespan_cycles"])
        baseline_copy = int(baseline["added_copy_bytes"])
        for candidate in group.get("candidates", []):
            metrics = candidate.get("metrics")
            if not candidate.get("legal") or not isinstance(metrics, Mapping):
                continue
            name = str(candidate["name"])
            families[name] = str(candidate.get("family"))
            paired.setdefault(name, []).append((
                int(metrics["makespan_cycles"]),
                int(metrics["added_copy_bytes"]),
                baseline_makespan,
                baseline_copy,
            ))
    rows: List[Dict[str, Any]] = []
    for name, observations in sorted(paired.items()):
        makespan_deltas = [item[0] - item[2] for item in observations]
        copy_deltas = [item[1] - item[3] for item in observations]
        relative = [(item[2] - item[0]) / item[2] for item in observations]
        improved = sum(delta < 0 for delta in makespan_deltas)
        worsened = sum(delta > 0 for delta in makespan_deltas)
        ties = len(observations) - improved - worsened
        rows.append({
            "candidate": name,
            "family": families[name],
            "paired_cells": len(observations),
            "makespan_improved_cells": improved,
            "makespan_tied_cells": ties,
            "makespan_worsened_cells": worsened,
            "distinct_non_ties": improved + worsened,
            "two_sided_sign_test_p": _two_sided_sign_test(
                improved, worsened),
            "mean_makespan_delta_cycles": statistics.fmean(makespan_deltas),
            "median_makespan_delta_cycles": statistics.median(makespan_deltas),
            "total_makespan_delta_cycles": sum(makespan_deltas),
            "mean_relative_makespan_improvement": statistics.fmean(relative),
            "mean_added_copy_delta_bytes": statistics.fmean(copy_deltas),
            "median_added_copy_delta_bytes": statistics.median(copy_deltas),
            "cells_with_lower_added_copy": sum(
                delta < 0 for delta in copy_deltas),
            "cells_with_higher_added_copy": sum(
                delta > 0 for delta in copy_deltas),
            "interpretation_guard": (
                "paired_descriptive_test_not_multiple_testing_corrected"),
        })
    return rows


def _runtime_summary(
    candidates: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Mapping[str, Any]]] = {}
    for row in candidates:
        grouped.setdefault(str(row["candidate"]), []).append(row)
    rows: List[Dict[str, Any]] = []
    for name, records in sorted(grouped.items()):
        def total(field: str) -> float:
            return sum(float(row[field] or 0.0) for row in records)

        cached_original_seconds = 0.0
        for row in records:
            try:
                source = json.loads(str(row.get("source") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                source = {}
            cached_original_seconds += float(
                source.get("cached_original_official_evaluation_seconds", 0.0))
        rows.append({
            "candidate": name,
            "records": len(records),
            "legal_records": sum(bool(row["legal"]) for row in records),
            "failed_records": sum(row["status"] == "failed" for row in records),
            "cache_hits": sum(bool(row["cache_hit"]) for row in records),
            "generation_seconds_sum": total("generation_seconds"),
            "validation_seconds_sum": total("validation_seconds"),
            "cache_lookup_seconds_sum": total("cache_lookup_seconds"),
            "official_evaluation_seconds_recorded_sum": total(
                "official_evaluation_seconds"),
            "cached_original_official_seconds_sum": cached_original_seconds,
            "invocation_wall_seconds_sum": total("invocation_wall_seconds"),
            "cold_cache_candidate_seconds_estimate": (
                total("invocation_wall_seconds")
                + cached_original_seconds
                + sum(
                    float(row["official_evaluation_seconds"] or 0.0)
                    for row in records if bool(row["cache_hit"])
                )),
        })
    return rows


def _summarize(
    *, repo: Path, output: Path, run_identity: Mapping[str, Any],
    requested_cases: Sequence[str], max_cores: int,
    invocation_failures: Sequence[Mapping[str, Any]],
    preflight_seconds: float,
    execution_seconds: float,
    wall_seconds: float,
) -> Dict[str, Any]:
    groups = _load_all_reusable_groups(
        repo, output, str(run_identity["run_identity_sha256"]))
    selected = _selected_rows(groups)
    candidates = _candidate_rows(groups)
    failures = [row for row in candidates if row["status"] == "failed"]
    speedups, aggregates = _speedup_and_aggregate(selected)
    paired_candidates = _paired_candidate_summary(groups)
    runtime = _runtime_summary(candidates)
    tail_degradation = [row for row in speedups if float(row["speedup"]) < 1.0]
    _write_csv(output / "selected_results.csv", selected)
    _write_csv(output / "candidate_results.csv", candidates)
    _write_csv(
        output / "failures.csv",
        failures + list(invocation_failures),
        empty_fields=(
            "case", "cores", "candidate", "failure_stage", "error_type",
            "error"),
    )
    _write_csv(output / "speedups.csv", speedups)
    _write_csv(output / "aggregate_by_core.csv", aggregates)
    _write_csv(output / "paired_candidate_summary.csv", paired_candidates)
    _write_csv(output / "runtime_by_candidate.csv", runtime)
    _write_csv(
        output / "tail_degradation.csv",
        tail_degradation,
        empty_fields=(
            "case", "cores", "single_makespan_cycles",
            "final_makespan_cycles", "speedup"),
    )

    requested_keys = {(case, cores) for case in requested_cases
                      for cores in range(1, max_cores + 1)}
    recorded_keys = {(str(group["case"]), int(group["cores"]))
                     for group in groups}
    complete = requested_keys.issubset(recorded_keys) and not invocation_failures
    missing_requested = requested_keys - recorded_keys
    final_deltas = [int(row["makespan_delta_vs_stage1"]) for row in selected
                    if int(row["cores"]) >= 2]
    if any(delta > 0 for delta in final_deltas):
        raise ValueError("final portfolio regressed below its Stage-1 fallback")

    keys = sorted({str(row["evaluation_key"]) for row in candidates
                   if row.get("evaluation_key")})
    key_summary = {
        "schema_version": 1,
        "run_identity_sha256": run_identity["run_identity_sha256"],
        "evaluation_key_count": len(keys),
        "evaluation_key_set_sha256": json_sha256(keys),
        "evaluation_keys": keys,
    }
    _write_json(output / "cache_key_summary.json", key_summary)
    winner_counts: Dict[str, int] = {}
    for row in selected:
        name = str(row["winner"])
        winner_counts[name] = winner_counts.get(name, 0) + 1
    summary = {
        "schema_version": 1,
        "kind": "problem2_final_portfolio_summary",
        "requested_cases": list(requested_cases),
        "requested_max_cores": max_cores,
        "requested_case_core_cells": len(requested_keys),
        "requested_complete": complete,
        "recorded_case_core_cells": len(groups),
        "full_500_complete": len(groups) == 500,
        "candidate_records": len(candidates),
        "candidate_failures": len(failures),
        "candidate_failure_rate": (
            len(failures) / len(candidates) if candidates else 0.0),
        "fatal_invocation_failures": len(invocation_failures),
        "missing_requested_case_core_cells": len(missing_requested),
        "requested_case_core_failure_rate": (
            len(missing_requested) / len(requested_keys)
            if requested_keys else 0.0),
        "winner_counts": winner_counts,
        "improved_vs_stage1_cells": sum(
            bool(row["improved_vs_stage1"]) for row in selected),
        "total_makespan_cycles_saved_vs_stage1": -sum(final_deltas),
        "mean_relative_improvement_vs_stage1": statistics.fmean([
            (-int(row["makespan_delta_vs_stage1"]) /
             int(row["stage1_makespan_cycles"]))
            for row in selected if int(row["cores"]) >= 2
        ]) if any(int(row["cores"]) >= 2 for row in selected) else None,
        "aggregate_by_core": aggregates,
        "paired_candidate_summary": paired_candidates,
        "tail_degradation_cells": len(tail_degradation),
        "worst_speedup_cells": sorted(
            speedups, key=lambda row: float(row["speedup"]))[:10],
        "runtime_by_candidate": runtime,
        "preflight_seconds": preflight_seconds,
        "execution_seconds": execution_seconds,
        "selection_rule": ["makespan_cycles", "added_copy_bytes"],
        "stage1_global_fallback_preserved": True,
        "run_identity_sha256": run_identity["run_identity_sha256"],
        "wall_seconds": wall_seconds,
        "timestamp": _utc_now(),
    }
    _write_json(output / "summary.json", summary)
    return summary


def main() -> int:
    end_to_end_started = time.perf_counter()
    end_to_end_started_at = _utc_now()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument(
        "--baseline", type=Path,
        default=Path("artifacts/problem2_stage1_baseline_cold_audit"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument(
        "--phase",
        choices=("smoke", "development", "validation", "holdout", "all", "custom"),
        default="smoke")
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--max-cores", type=int, default=5)
    parser.add_argument("--workers", type=int, default=min(24, os.cpu_count() or 1))
    parser.add_argument("--max-moves", type=int, default=2)
    parser.add_argument("--search-width", type=int, default=12)
    args = parser.parse_args()

    repo = args.repo.resolve()
    baseline = (args.baseline.resolve() if args.baseline.is_absolute()
                else (repo / args.baseline).resolve())
    output = (args.output.resolve() if args.output.is_absolute()
              else (repo / args.output).resolve())
    cache = (args.cache.resolve() if args.cache.is_absolute()
             else (repo / args.cache).resolve())
    split_path = repo / "problem2_preregistered_split.json"
    if not baseline.is_dir():
        parser.error("verified Stage-1 baseline directory is missing")
    if not split_path.is_file():
        parser.error("problem2_preregistered_split.json is missing")
    if args.max_cores < 1 or args.max_cores > 5:
        parser.error("--max-cores must be in 1..5")
    if args.workers < 1:
        parser.error("--workers must be positive")
    split = _load_split(split_path)
    if (args.max_moves != int(split["mapping_max_moves"])
            or args.search_width != int(split["mapping_search_width"])):
        parser.error("mapping settings are frozen to max_moves=2, search_width=12")
    try:
        cases = _phase_cases(split, args.phase, args.cases)
    except ValueError as error:
        parser.error(str(error))
    official_cases = set(_discover_cases(repo))
    unknown = sorted(set(cases) - official_cases)
    if unknown:
        parser.error("unknown official cases: {}".format(unknown))
    if len(set(cases)) != len(cases):
        parser.error("case list contains duplicates")

    run_identity, implementation_hash = _build_run_identity(
        repo=repo, baseline=baseline, split_path=split_path)
    output.mkdir(parents=True, exist_ok=True)
    identity_path = output / "run_identity.json"
    if identity_path.is_file():
        old = _read_json(identity_path)
        if old != run_identity:
            parser.error(
                "output belongs to a different run identity; use a new directory")
    else:
        _write_json(identity_path, run_identity)

    preflight_seconds = time.perf_counter() - end_to_end_started
    execution_started = time.perf_counter()
    invocation_id = "{}-pid{}".format(
        end_to_end_started_at.replace("-", "").replace(":", "").replace(
            "+", "p"),
        os.getpid())
    payloads = [{
        "graph_path": str(repo / "data" / (case + ".json")),
        "config_path": str(repo / "data" / "config.txt"),
        "baseline": str(baseline),
        "output": str(output),
        "cache": str(cache),
        "max_cores": args.max_cores,
        "max_moves": args.max_moves,
        "search_width": args.search_width,
        "run_identity_sha256": run_identity["run_identity_sha256"],
        "implementation_hash": implementation_hash,
    } for case in cases]
    invocation_groups: List[Dict[str, Any]] = []
    invocation_failures: List[Dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=min(args.workers, len(payloads))) as executor:
        future_by_case = {
            executor.submit(_run_case_payload, payload):
                Path(payload["graph_path"]).stem
            for payload in payloads
        }
        for future in as_completed(future_by_case):
            case = future_by_case[future]
            result = future.result()
            invocation_groups.extend(result["groups"])
            invocation_failures.extend(result["failures"])
            if result["fatal"] is not None:
                invocation_failures.append(result["fatal"])
                print("{}: failed {}".format(case, result["fatal"]["error"]),
                      file=sys.stderr, flush=True)
            else:
                executed = sum(not bool(group.get("_resume"))
                               for group in result["groups"])
                resumed = len(result["groups"]) - executed
                print("{}: success executed={} resumed={}".format(
                    case, executed, resumed), flush=True)
            _write_json(output / "progress.json", {
                "phase": args.phase,
                "requested_cases": list(cases),
                "finished_cases": len({str(group["case"])
                                       for group in invocation_groups}),
                "invocation_failures": invocation_failures,
                "execution_elapsed_seconds": (
                    time.perf_counter() - execution_started),
                "end_to_end_elapsed_seconds": (
                    time.perf_counter() - end_to_end_started),
                "timestamp": _utc_now(),
            })

    execution_seconds = time.perf_counter() - execution_started
    wall_seconds = time.perf_counter() - end_to_end_started
    summary = _summarize(
        repo=repo,
        output=output,
        run_identity=run_identity,
        requested_cases=cases,
        max_cores=args.max_cores,
        invocation_failures=[failure for failure in invocation_failures
                             if failure.get("candidate") == "*case_infrastructure*"],
        preflight_seconds=preflight_seconds,
        execution_seconds=execution_seconds,
        wall_seconds=wall_seconds,
    )
    invocation = {
        "invocation_id": invocation_id,
        "started_at": end_to_end_started_at,
        "finished_at": _utc_now(),
        "phase": args.phase,
        "requested_cases": list(cases),
        "max_cores": args.max_cores,
        "run_identity_sha256": run_identity["run_identity_sha256"],
        "executed_groups": sum(not bool(group.get("_resume"))
                               for group in invocation_groups),
        "resumed_groups": sum(bool(group.get("_resume"))
                              for group in invocation_groups),
        "candidate_failures": len([
            failure for failure in invocation_failures
            if failure.get("candidate") != "*case_infrastructure*"]),
        "fatal_failures": len([
            failure for failure in invocation_failures
            if failure.get("candidate") == "*case_infrastructure*"]),
        "requested_complete": summary["requested_complete"],
        "preflight_seconds": preflight_seconds,
        "execution_seconds": execution_seconds,
        "wall_seconds": wall_seconds,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
    }
    _write_json(output / "invocations" / invocation_id / "summary.json", invocation)
    _append_history(output, invocation)
    print(json.dumps({
        "complete": summary["requested_complete"],
        "recorded_case_core_cells": summary["recorded_case_core_cells"],
        "full_500_complete": summary["full_500_complete"],
        "improved_vs_stage1_cells": summary["improved_vs_stage1_cells"],
        "candidate_failures": summary["candidate_failures"],
        "fatal_failures": summary["fatal_invocation_failures"],
    }, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if summary["requested_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
