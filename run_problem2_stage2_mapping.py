#!/usr/bin/env python3
"""Resumable Stage-2 fixed-partition mapping experiments for Problem 2.

The baseline result of each partition is imported only after content/hash
validation.  Newly mapped candidates are scored exclusively by the original
official Scene-B evaluator.  Work is parallelized by (case, core count), while
the five partitions within a cell run serially to avoid cache write races.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from candidate_manager_problem2 import json_sha256
from candidate_manager_problem1 import sha256_file
from candidate_manager_problem2_stage2 import (
    MAPPING_FAMILIES,
    MAPPING_POLICIES,
    Stage1SelectedReference,
    load_verified_stage1_selected_references,
    normalize_mapping_policies,
    reusable_stage2_group,
    run_stage2_mapping_group,
    stage2_implementation_hash,
)


GATE_A_CASE_CORES = (("case_050", 4), ("case_100", 5))
GATE_A_FAMILIES = ("b2a_w16",)
GATE_B_CASES = ("case_001", "case_050", "case_100")
GATE_B_CORES = (2, 4, 5)
STRATIFIED_12_CASES = (
    "case_001", "case_016", "case_024", "case_044",
    "case_047", "case_048", "case_050", "case_051",
    "case_058", "case_064", "case_067", "case_100",
)
FROZEN_STRATIFIED_MAPPING_POLICY = "locality"
MAPPING_POLICY_ARGUMENTS = tuple(
    "map_{}".format(policy) for policy in MAPPING_POLICIES)
STRATUM_REPEATED = "repeated_gate_b"
STRATUM_SEEN_NEW_CORE = "seen_case_new_core"
STRATUM_UNSEEN = "unseen_case"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fieldnames.append(field)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _invocation_id(started_at: str) -> str:
    compact = (started_at.replace("-", "").replace(":", "")
               .replace("+", "p").replace(".", "_"))
    return "{}-pid{}".format(compact, os.getpid())


def _append_invocation_history(output: Path, record: Mapping[str, Any]) -> None:
    history_path = output / "invocation_history.json"
    if history_path.is_file():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            history = None
    else:
        history = None
    if not isinstance(history, dict) or not isinstance(
            history.get("invocations"), list):
        history = {
            "schema_version": 1,
            "kind": "problem2_stage2_mapping_invocation_history",
            "invocations": [],
        }
    history["invocations"].append(dict(record))
    history["latest_invocation_id"] = record["invocation_id"]
    history["updated_at"] = _utc_now()
    _write_json(history_path, history)


def _parse_csv_values(value: str, cast=str) -> Tuple[Any, ...]:
    items = tuple(cast(item.strip()) for item in value.split(",") if item.strip())
    if not items:
        raise argparse.ArgumentTypeError("list cannot be empty")
    return items


def _discover_cases(repo: Path) -> Tuple[str, ...]:
    pattern = re.compile(r"case_[0-9]{3}")
    return tuple(
        path.stem for path in sorted((repo / "data").glob("case_*.json"))
        if pattern.fullmatch(path.stem)
    )


def _resolve_mapping_policies(argument: str) -> Tuple[str, ...]:
    if argument == "all":
        return tuple(MAPPING_POLICIES)
    prefix = "map_"
    if not argument.startswith(prefix):
        raise ValueError("mapping policy must use the map_<policy> name")
    return normalize_mapping_policies((argument[len(prefix):],))


def _validate_preset_budget(preset: str, mapping_policies: Sequence[str]) -> None:
    policies = tuple(mapping_policies)
    if preset == "stratified12" and policies != (
            FROZEN_STRATIFIED_MAPPING_POLICY,):
        raise ValueError(
            "stratified12 is budget-locked unless using the pre-registered "
            "unique policy "
            "--mapping-policy map_locality; do not run multiple policies")
    if preset in {"gate-a", "gate-b"} and policies != tuple(MAPPING_POLICIES):
        raise ValueError(
            "{} reproduces the three-policy design and requires "
            "--mapping-policy all".format(preset))


def _report_stratum(case: str, cores: int) -> str:
    if case in GATE_B_CASES:
        if cores in GATE_B_CORES:
            return STRATUM_REPEATED
        return STRATUM_SEEN_NEW_CORE
    return STRATUM_UNSEEN


def _process_exit_code(
    summary: Mapping[str, Any], failures: Sequence[Mapping[str, Any]],
) -> int:
    if not summary.get("complete") or failures:
        return 1
    if not summary.get("continue_gate", {}).get("passed"):
        return 2
    return 0


def _resolve_design(
    preset: str,
    cases_arg: str | None,
    cores_arg: str | None,
    families_arg: str | None,
    repo: Path,
) -> Tuple[Tuple[str, ...], Tuple[int, ...], Tuple[str, ...], set[Tuple[str, int]] | None]:
    allowed_pairs: set[Tuple[str, int]] | None = None
    if preset == "gate-a":
        cases = tuple(sorted({case for case, _ in GATE_A_CASE_CORES}))
        cores = tuple(sorted({cores for _, cores in GATE_A_CASE_CORES}))
        families = GATE_A_FAMILIES
        allowed_pairs = set(GATE_A_CASE_CORES)
    elif preset == "gate-b":
        cases, cores, families = GATE_B_CASES, GATE_B_CORES, MAPPING_FAMILIES
    elif preset == "stratified12":
        cases, cores, families = STRATIFIED_12_CASES, (2, 3, 4, 5), MAPPING_FAMILIES
    elif preset == "custom":
        cases = (_parse_csv_values(cases_arg) if cases_arg
                 else _discover_cases(repo))
        cores = (_parse_csv_values(cores_arg, int) if cores_arg
                 else (2, 3, 4, 5))
        families = (_parse_csv_values(families_arg) if families_arg
                    else MAPPING_FAMILIES)
    else:
        raise ValueError("unknown preset")
    unknown = sorted(set(families) - set(MAPPING_FAMILIES))
    if unknown:
        raise ValueError("unknown partition families: {}".format(unknown))
    missing = [case for case in cases if not (repo / "data" / (case + ".json")).is_file()]
    if missing:
        raise FileNotFoundError("case files are missing: {}".format(missing))
    if any(core < 2 or core > 5 for core in cores):
        raise ValueError("Stage-2 pilot core counts must be in 2..5")
    return tuple(cases), tuple(cores), tuple(families), allowed_pairs


def _run_cell(payload: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    repo = Path(payload["repo"])
    output = Path(payload["output"])
    groups: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for family in payload["families"]:
        try:
            group_path = (
                output / "groups" / payload["case"]
                / "k{}".format(payload["cores"]) / (family + ".json")
            )
            reusable = reusable_stage2_group(
                group_path=group_path,
                graph_path=repo / "data" / (payload["case"] + ".json"),
                config_path=repo / "data" / "config.txt",
                baseline_root=Path(payload["baseline"]),
                cores=int(payload["cores"]),
                family=family,
                max_moves=int(payload["max_moves"]),
                search_width=int(payload["search_width"]),
                mapping_policies=tuple(payload["mapping_policies"]),
            )
            if reusable is not None:
                found = dict(reusable)
                found["_resume"] = True
                groups.append(found)
                continue
            result = run_stage2_mapping_group(
                graph_path=repo / "data" / (payload["case"] + ".json"),
                config_path=repo / "data" / "config.txt",
                baseline_root=Path(payload["baseline"]),
                output_root=output,
                cache_dir=Path(payload["cache"]),
                cores=int(payload["cores"]),
                family=family,
                max_moves=int(payload["max_moves"]),
                search_width=int(payload["search_width"]),
                mapping_policies=tuple(payload["mapping_policies"]),
            )
            found = dict(result.manifest)
            found["_resume"] = False
            groups.append(found)
        except Exception as error:
            failures.append({
                "case": payload["case"],
                "cores": int(payload["cores"]),
                "partition_family": family,
                "error_type": type(error).__name__,
                "error": "{}: {}".format(type(error).__name__, error),
            })
    return {"groups": groups, "failures": failures}


def _candidate_rows(groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in sorted(groups, key=lambda item: (
            item["case"], int(item["cores"]), item["partition_family"])):
        for record in group.get("candidates", []):
            metrics = record.get("metrics") or {}
            timing = record.get("timing") or {}
            rows.append({
                "case": group["case"],
                "cores": group["cores"],
                "report_stratum": _report_stratum(
                    str(group["case"]), int(group["cores"])),
                "partition_family": group["partition_family"],
                "candidate": record.get("name"),
                "mapping_policy": record.get("policy"),
                "status": record.get("status"),
                "legal": record.get("legal"),
                "deduplicated": record.get("deduplicated"),
                "canonical_candidate": record.get("canonical_candidate"),
                "cache_hit": record.get("cache_hit", False),
                "failure_stage": record.get("failure_stage"),
                "error_type": record.get("error_type"),
                "error": record.get("error"),
                "makespan_cycles": metrics.get("makespan_cycles"),
                "added_copy_bytes": metrics.get("added_copy_bytes"),
                "partition_added_copy_bytes": metrics.get("partition_added_copy_bytes"),
                "spill_added_copy_bytes": metrics.get("spill_added_copy_bytes"),
                "cross_task_traffic_bytes": metrics.get("cross_task_traffic_bytes"),
                "cross_core_transfer_count": metrics.get("cross_core_transfer_count"),
                "used_core_count": metrics.get("used_core_count"),
                "memory_peak_by_core": json.dumps(
                    metrics.get("memory_peak_by_core"), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")),
                "per_core_utilization": json.dumps(
                    metrics.get("per_core_utilization"), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")),
                "generation_seconds": timing.get("generation_seconds"),
                "validation_seconds": timing.get("validation_seconds"),
                "cache_lookup_seconds": timing.get("cache_lookup_seconds"),
                "official_evaluation_seconds": timing.get("official_evaluation_seconds"),
                "invocation_wall_seconds": timing.get("invocation_wall_seconds"),
                "plan_hash": record.get("plan_hash"),
                "canonical_signature": record.get("canonical_signature"),
                "evaluation_key": record.get("evaluation_key"),
                "official_result_json_sha256": record.get(
                    "official_result_json_sha256"),
            })
    return rows


def _paired_rows(groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in sorted(groups, key=lambda item: (
            item["case"], int(item["cores"]), item["partition_family"])):
        baseline = group["baseline"]["metrics"]
        configured = tuple(group.get("mapping_policies") or (
            str(candidate.get("policy"))
            for candidate in group.get("candidates", [])[1:]
            if candidate.get("policy")
        ))
        expected_names = {"map_{}".format(policy) for policy in configured}
        mapping_records = [
            candidate for candidate in group.get("candidates", [])[1:]
            if candidate.get("name") in expected_names
        ]
        mapped = [candidate for candidate in mapping_records
                  if candidate.get("legal") and candidate.get("metrics")]
        best = min(mapped, key=lambda candidate: (
            int(candidate["metrics"]["makespan_cycles"]),
            int(candidate["metrics"]["added_copy_bytes"]),
            str(candidate["name"]),
        )) if mapped else None
        old = int(baseline["makespan_cycles"])
        old_added = int(baseline["added_copy_bytes"])
        new = int(best["metrics"]["makespan_cycles"]) if best else None
        new_added = int(best["metrics"]["added_copy_bytes"]) if best else None
        score_win = (best is not None and (new, new_added) < (old, old_added))
        rows.append({
            "case": group["case"],
            "cores": group["cores"],
            "report_stratum": _report_stratum(
                str(group["case"]), int(group["cores"])),
            "partition_family": group["partition_family"],
            "baseline_makespan_cycles": old,
            "best_mapping_candidate": best["name"] if best else None,
            "best_mapping_makespan_cycles": new,
            "makespan_delta_cycles": new - old if new is not None else None,
            "relative_improvement": ((old - new) / old
                                     if new is not None and old else None),
            "baseline_added_copy_bytes": old_added,
            "best_mapping_added_copy_bytes": new_added,
            "added_copy_delta_bytes": (
                new_added - old_added if new_added is not None else None),
            "baseline_spill_added_copy_bytes": baseline["spill_added_copy_bytes"],
            "best_mapping_spill_added_copy_bytes": (
                best["metrics"]["spill_added_copy_bytes"] if best else None),
            "strict_mapping_win": score_win,
            "strict_mapping_score_win": score_win,
            "strict_mapping_makespan_win": new is not None and new < old,
            "all_mapping_candidates_legal": (
                bool(configured)
                and len(mapping_records) == len(configured)
                and len(mapped) == len(configured)),
        })
    return rows


def _mapping_records(
    cell_groups: Sequence[Mapping[str, Any]], policy: str | None = None,
) -> List[Tuple[Mapping[str, Any], Mapping[str, Any]]]:
    expected_name = None if policy is None else "map_{}".format(policy)
    return [
        (group, candidate)
        for group in cell_groups
        for candidate in group.get("candidates", [])[1:]
        if candidate.get("legal") and candidate.get("metrics")
        and (expected_name is None or candidate.get("name") == expected_name)
    ]


def _case_core_selection_row(
    *,
    case: str,
    cores: int,
    cell_groups: Sequence[Mapping[str, Any]],
    stage1: Stage1SelectedReference,
    policy: str | None = None,
) -> Dict[str, Any]:
    mappings = _mapping_records(cell_groups, policy)
    best_pair = min(mappings, key=lambda item: (
        int(item[1]["metrics"]["makespan_cycles"]),
        int(item[1]["metrics"]["added_copy_bytes"]),
        str(item[0]["partition_family"]),
        str(item[1]["name"]),
    )) if mappings else None
    baseline_metrics = stage1.metrics
    baseline_score = stage1.score()
    if best_pair is None:
        best_group = best_candidate = None
        mapped_score = None
    else:
        best_group, best_candidate = best_pair
        mapped_score = (
            int(best_candidate["metrics"]["makespan_cycles"]),
            int(best_candidate["metrics"]["added_copy_bytes"]),
        )
    use_mapping = mapped_score is not None and mapped_score < baseline_score
    final_metrics = (best_candidate["metrics"] if use_mapping
                     else baseline_metrics)
    final_makespan = int(final_metrics["makespan_cycles"])
    baseline_makespan = int(baseline_metrics["makespan_cycles"])
    makespan_delta = final_makespan - baseline_makespan
    improvement = ((baseline_makespan - final_makespan) / baseline_makespan
                   if baseline_makespan else 0.0)
    return {
        "case": case,
        "cores": cores,
        "report_stratum": _report_stratum(case, cores),
        **({"mapping_policy": policy} if policy is not None else {}),
        "stage1_winner": stage1.winner,
        "stage1_makespan_cycles": baseline_makespan,
        "stage1_added_copy_bytes": int(baseline_metrics["added_copy_bytes"]),
        "stage1_spill_added_copy_bytes": int(
            baseline_metrics["spill_added_copy_bytes"]),
        "best_mapping_family": (
            str(best_group["partition_family"]) if best_group else None),
        "best_mapping_candidate": (
            str(best_candidate["name"]) if best_candidate else None),
        "best_mapping_makespan_cycles": (
            int(best_candidate["metrics"]["makespan_cycles"])
            if best_candidate else None),
        "best_mapping_added_copy_bytes": (
            int(best_candidate["metrics"]["added_copy_bytes"])
            if best_candidate else None),
        "final_source": "mapping" if use_mapping else "stage1_fallback",
        "final_winner": (
            str(best_candidate["name"]) if use_mapping else stage1.winner),
        "final_partition_family": (
            str(best_group["partition_family"]) if use_mapping else None),
        "final_makespan_cycles": final_makespan,
        "final_added_copy_bytes": int(final_metrics["added_copy_bytes"]),
        "final_spill_added_copy_bytes": int(
            final_metrics["spill_added_copy_bytes"]),
        "makespan_delta_cycles": makespan_delta,
        "added_copy_delta_bytes": (
            int(final_metrics["added_copy_bytes"])
            - int(baseline_metrics["added_copy_bytes"])),
        "spill_delta_bytes": (
            int(final_metrics["spill_added_copy_bytes"])
            - int(baseline_metrics["spill_added_copy_bytes"])),
        "relative_improvement": improvement,
        "score_improved": use_mapping,
        "makespan_improved": makespan_delta < 0,
        "improved_at_least_1pct": improvement >= 0.01,
    }


def _case_core_rows(
    groups: Sequence[Mapping[str, Any]],
    stage1_selected: Mapping[Tuple[str, int], Stage1SelectedReference],
) -> List[Dict[str, Any]]:
    cells: Dict[Tuple[str, int], List[Mapping[str, Any]]] = {}
    for group in groups:
        cells.setdefault((str(group["case"]), int(group["cores"])), []).append(group)
    rows: List[Dict[str, Any]] = []
    for (case, cores), cell_groups in sorted(cells.items()):
        try:
            stage1 = stage1_selected[(case, cores)]
        except KeyError as error:
            raise ValueError(
                "missing Stage-1 selected fallback for {} k{}".format(
                    case, cores)) from error
        rows.append(_case_core_selection_row(
            case=case,
            cores=cores,
            cell_groups=cell_groups,
            stage1=stage1,
        ))
    return rows


def _policy_case_core_rows(
    groups: Sequence[Mapping[str, Any]],
    stage1_selected: Mapping[Tuple[str, int], Stage1SelectedReference],
    mapping_policies: Sequence[str],
) -> List[Dict[str, Any]]:
    cells: Dict[Tuple[str, int], List[Mapping[str, Any]]] = {}
    for group in groups:
        cells.setdefault((str(group["case"]), int(group["cores"])), []).append(group)
    rows: List[Dict[str, Any]] = []
    for (case, cores), cell_groups in sorted(cells.items()):
        stage1 = stage1_selected[(case, cores)]
        for policy in mapping_policies:
            rows.append(_case_core_selection_row(
                case=case,
                cores=cores,
                cell_groups=cell_groups,
                stage1=stage1,
                policy=policy,
            ))
    return rows


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def _selection_aggregate(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    relative = [float(row["relative_improvement"]) for row in rows]
    savings = [-int(row["makespan_delta_cycles"]) for row in rows]
    return {
        "case_core_cells": len(rows),
        "score_improved_cells": sum(bool(row["score_improved"]) for row in rows),
        "makespan_improved_cells": sum(
            bool(row["makespan_improved"]) for row in rows),
        "improved_at_least_1pct_cells": sum(
            bool(row["improved_at_least_1pct"]) for row in rows),
        "total_makespan_cycles_saved": sum(savings),
        "mean_relative_improvement": (
            statistics.fmean(relative) if relative else None),
        "median_relative_improvement": (
            statistics.median(relative) if relative else None),
        "max_relative_improvement": max(relative) if relative else None,
        "mean_added_copy_delta_bytes": (
            statistics.fmean(float(row["added_copy_delta_bytes"]) for row in rows)
            if rows else None),
        "mean_spill_delta_bytes": (
            statistics.fmean(float(row["spill_delta_bytes"]) for row in rows)
            if rows else None),
    }


def summarize(
    *,
    output: Path,
    groups: Sequence[Mapping[str, Any]],
    expected_groups: int,
    preset: str,
    mapping_policies: Sequence[str],
    stage1_selected: Mapping[Tuple[str, int], Stage1SelectedReference],
    run_identity: Mapping[str, Any],
    wall_seconds: float,
) -> Dict[str, Any]:
    candidates = _candidate_rows(groups)
    paired = _paired_rows(groups)
    case_core = _case_core_rows(groups, stage1_selected)
    policy_case_core = _policy_case_core_rows(
        groups, stage1_selected, mapping_policies)
    failures = [row for row in candidates if row["status"] == "failed"]
    _write_csv(output / "candidate_results.csv", candidates)
    _write_csv(output / "paired_mapping_results.csv", paired)
    _write_csv(output / "case_core_results.csv", case_core)
    _write_csv(output / "policy_case_core_results.csv", policy_case_core)
    _write_csv(output / "failures.csv", failures)

    deltas = [int(row["makespan_delta_cycles"]) for row in paired
              if row["makespan_delta_cycles"] is not None]
    strict_wins = sum(bool(row["strict_mapping_win"]) for row in paired)
    strict_makespan_wins = sum(
        bool(row["strict_mapping_makespan_win"]) for row in paired)
    legal_groups = sum(bool(row["all_mapping_candidates_legal"]) for row in paired)
    improved_cells = sum(bool(row["improved_at_least_1pct"]) for row in case_core)
    complete = len(groups) == expected_groups
    gate_common = complete and legal_groups == expected_groups and not failures
    group_strata = {
        stratum: sum(
            1 for group in groups
            if _report_stratum(str(group["case"]), int(group["cores"])) == stratum)
        for stratum in (
            STRATUM_REPEATED, STRATUM_SEEN_NEW_CORE, STRATUM_UNSEEN)
    }
    cell_rows_by_stratum = {
        stratum: [row for row in case_core if row["report_stratum"] == stratum]
        for stratum in (
            STRATUM_REPEATED, STRATUM_SEEN_NEW_CORE, STRATUM_UNSEEN)
    }
    stratum_results = {
        stratum: {
            "groups": group_strata[stratum],
            **_selection_aggregate(rows),
        }
        for stratum, rows in cell_rows_by_stratum.items()
    }
    policy_results = {
        policy: _selection_aggregate([
            row for row in policy_case_core
            if row["mapping_policy"] == policy
        ])
        for policy in mapping_policies
    }
    design_official_evaluations = sum(
        int(group.get("official_evaluations", 0)) for group in groups)
    invocation_official_evaluations = sum(
        int(group.get("official_evaluations", 0))
        for group in groups if not group.get("_resume"))
    if preset == "gate-a":
        gate_pass = gate_common and design_official_evaluations <= 10
        criterion = "complete, every mapped candidate legal, zero failures, <=10 official evaluations"
    elif preset == "gate-b":
        gate_pass = gate_common and (strict_wins >= 9 or improved_cells >= 3)
        criterion = ("45/45 groups legal, zero failures, and >=9 strict "
                     "(makespan, added-copy) wins or >=3/9 final case-core "
                     "cells improve Makespan >=1% versus Stage-1 selected winners")
    elif preset == "stratified12":
        strata_match = group_strata == {
            STRATUM_REPEATED: 45,
            STRATUM_SEEN_NEW_CORE: 15,
            STRATUM_UNSEEN: 180,
        } and {name: len(rows) for name, rows in cell_rows_by_stratum.items()} == {
            STRATUM_REPEATED: 9,
            STRATUM_SEEN_NEW_CORE: 3,
            STRATUM_UNSEEN: 36,
        }
        gate_pass = gate_common and strata_match
        criterion = ("240/240 map_locality groups legal and zero failures; "
                     "report 45 repeated, 15 seen-case/new-core, and 180 "
                     "unseen-case groups separately; primary efficacy metric "
                     "is the 36 unseen-case final case-core selections versus "
                     "Stage-1 selected winners")
    else:
        gate_pass = gate_common
        criterion = "complete, every mapped candidate legal, zero failures"
    summary = {
        "schema_version": 2,
        "kind": "problem2_stage2_mapping_summary",
        "preset": preset,
        "expected_groups": expected_groups,
        "recorded_groups": len(groups),
        "complete": complete,
        "mapping_candidates": len(candidates) - len(groups),
        "official_evaluations": design_official_evaluations,
        "invocation_official_evaluations": invocation_official_evaluations,
        "cache_hits": sum(int(group.get("cache_hits", 0)) for group in groups),
        "resumed_groups": sum(bool(group.get("_resume")) for group in groups),
        "failed_candidates": len(failures),
        "all_mapping_candidates_legal_groups": legal_groups,
        "strict_mapping_wins": strict_wins,
        "strict_mapping_makespan_wins": strict_makespan_wins,
        "case_core_cells": len(case_core),
        "case_core_improved_at_least_1pct": improved_cells,
        "case_core_selection": _selection_aggregate(case_core),
        "policy_case_core_selection": policy_results,
        "report_strata": stratum_results,
        "reporting_contract": {
            "selection_score": ["makespan_cycles", "added_copy_bytes"],
            "global_fallback": "Stage-1 selected_results.csv actual winner",
            "stratified12_mapping_policy": "map_locality",
            "expected_group_strata": {
                STRATUM_REPEATED: 45,
                STRATUM_SEEN_NEW_CORE: 15,
                STRATUM_UNSEEN: 180,
            },
            "expected_case_core_strata": {
                STRATUM_REPEATED: 9,
                STRATUM_SEEN_NEW_CORE: 3,
                STRATUM_UNSEEN: 36,
            },
            "primary_metric": (
                "36 unseen-case final case-core selections relative to "
                "Stage-1 actual winners"),
        },
        "paired_delta_cycles": {
            "mean": statistics.fmean(deltas) if deltas else None,
            "median": statistics.median(deltas) if deltas else None,
            "p90": _quantile(deltas, 0.90),
            "worst": max(deltas) if deltas else None,
        },
        "continue_gate": {
            "passed": gate_pass,
            "criterion": criterion,
        },
        "wall_seconds": wall_seconds,
        "run_identity": dict(run_identity),
        "timestamp": _utc_now(),
    }
    _write_json(output / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--baseline", type=Path,
                        default=Path("artifacts/problem2_stage1_baseline_cold_audit"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--preset", choices=("gate-a", "gate-b", "stratified12", "custom"),
                        default="gate-b")
    parser.add_argument("--cases")
    parser.add_argument("--cores")
    parser.add_argument("--families")
    parser.add_argument(
        "--mapping-policy",
        choices=("all",) + MAPPING_POLICY_ARGUMENTS,
        default="all",
        help=("mapping candidate set; stratified12 is pre-registered to "
              "map_locality only"),
    )
    parser.add_argument("--workers", type=int, default=min(32, os.cpu_count() or 1))
    parser.add_argument("--max-moves", type=int, default=2)
    parser.add_argument("--search-width", type=int, default=12)
    args = parser.parse_args()

    started_at = _utc_now()
    invocation_id = _invocation_id(started_at)
    try:
        mapping_policies = _resolve_mapping_policies(args.mapping_policy)
        _validate_preset_budget(args.preset, mapping_policies)
    except ValueError as error:
        parser.error(str(error))
    repo = args.repo.resolve()
    baseline = (repo / args.baseline).resolve() if not args.baseline.is_absolute() else args.baseline.resolve()
    output = (repo / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    cache = (repo / args.cache).resolve() if not args.cache.is_absolute() else args.cache.resolve()
    if not (repo / "data" / "config.txt").is_file():
        parser.error("repository/data/config.txt is missing")
    if not baseline.is_dir():
        parser.error("verified Stage-1 baseline directory is missing")
    if args.workers < 1:
        parser.error("--workers must be positive")
    if args.max_moves < 0 or args.search_width < 1:
        parser.error("invalid bounded-search settings")
    cases, cores, families, allowed_pairs = _resolve_design(
        args.preset, args.cases, args.cores, args.families, repo)
    pairs = [(case, core) for case in cases for core in cores
             if allowed_pairs is None or (case, core) in allowed_pairs]
    expected_groups = len(pairs) * len(families)
    try:
        selected_snapshot = load_verified_stage1_selected_references(
            graph_root=repo / "data",
            config_path=repo / "data" / "config.txt",
            baseline_root=baseline,
            pairs=pairs,
        )
    except Exception as error:
        parser.error("Stage-1 selected fallback validation failed: {}".format(error))
    implementation_hash, implementation_files = stage2_implementation_hash()
    run_identity = {
        "implementation_sha256": implementation_hash,
        "implementation_file_sha256": implementation_files,
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "preset": args.preset,
        "cases": list(cases),
        "cores": list(cores),
        "families": list(families),
        "mapping_policy_argument": args.mapping_policy,
        "mapping_policies": list(mapping_policies),
        "pairs": [[case, core] for case, core in pairs],
        "max_moves": args.max_moves,
        "search_width": args.search_width,
        "baseline": str(baseline),
        "stage1_selected_results": str(
            selected_snapshot.selected_results_path),
        "stage1_selected_results_file_sha256": (
            selected_snapshot.selected_results_file_hash),
        "stage1_selected_reference_sha256": (
            selected_snapshot.selected_reference_hash),
        "cache": str(cache),
    }
    run_identity["run_identity_sha256"] = json_sha256(run_identity)
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "run_identity.json", run_identity)
    started = time.perf_counter()
    groups: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    payloads = [{
        "repo": str(repo),
        "baseline": str(baseline),
        "output": str(output),
        "cache": str(cache),
        "case": case,
        "cores": core,
        "families": list(families),
        "mapping_policies": list(mapping_policies),
        "max_moves": args.max_moves,
        "search_width": args.search_width,
    } for case, core in pairs]
    with ProcessPoolExecutor(max_workers=min(args.workers, len(payloads))) as executor:
        future_by_pair = {
            executor.submit(_run_cell, payload): (payload["case"], payload["cores"])
            for payload in payloads
        }
        for future in as_completed(future_by_pair):
            case, core = future_by_pair[future]
            try:
                cell = future.result()
                found = cell["groups"]
                groups.extend(found)
                failures.extend(cell["failures"])
                statuses = ",".join(
                    "{}:{}".format(group["partition_family"], group["winner"]["name"])
                    for group in found)
                if cell["failures"]:
                    failed_names = ",".join(
                        str(item["partition_family"])
                        for item in cell["failures"])
                    print("{} k{}: partial [{}], failed [{}]".format(
                        case, core, statuses, failed_names), file=sys.stderr, flush=True)
                else:
                    print("{} k{}: success [{}]".format(
                        case, core, statuses), flush=True)
            except Exception as error:
                failure = {
                    "case": case,
                    "cores": core,
                    "partition_family": "*cell_infrastructure*",
                    "error_type": type(error).__name__,
                    "error": "{}: {}".format(type(error).__name__, error),
                }
                failures.append(failure)
                print("{} k{}: failed {}".format(case, core, failure["error"]),
                      file=sys.stderr, flush=True)
            _write_json(output / "progress.json", {
                "expected_groups": expected_groups,
                "completed_groups": len(groups),
                "failed_cells": failures,
                "elapsed_seconds": time.perf_counter() - started,
                "timestamp": _utc_now(),
            })
    summary = summarize(
        output=output,
        groups=groups,
        expected_groups=expected_groups,
        preset=args.preset,
        mapping_policies=mapping_policies,
        stage1_selected=selected_snapshot.references,
        run_identity=run_identity,
        wall_seconds=time.perf_counter() - started,
    )
    summary["failed_cells"] = failures
    summary["latest_invocation_id"] = invocation_id
    _write_json(output / "summary.json", summary)
    invocation_record = {
        "invocation_id": invocation_id,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "preset": args.preset,
        "mapping_policies": list(mapping_policies),
        "run_identity_sha256": run_identity["run_identity_sha256"],
        "expected_groups": expected_groups,
        "recorded_groups": len(groups),
        "executed_groups": sum(not bool(group.get("_resume")) for group in groups),
        "resumed_groups": sum(bool(group.get("_resume")) for group in groups),
        "invocation_official_evaluations": summary[
            "invocation_official_evaluations"],
        "design_official_evaluations": summary["official_evaluations"],
        "failed_cells": failures,
        "complete": summary["complete"] and not failures,
        "gate_passed": summary["continue_gate"]["passed"],
        "wall_seconds": summary["wall_seconds"],
    }
    invocation_path = output / "invocations" / invocation_id / "summary.json"
    _write_json(invocation_path, invocation_record)
    _append_invocation_history(output, invocation_record)
    print(json.dumps({
        "complete": summary["complete"],
        "groups": summary["recorded_groups"],
        "official_evaluations": summary["official_evaluations"],
        "invocation_official_evaluations": summary[
            "invocation_official_evaluations"],
        "strict_mapping_wins": summary["strict_mapping_wins"],
        "case_core_improved_at_least_1pct": summary[
            "case_core_improved_at_least_1pct"],
        "gate_passed": summary["continue_gate"]["passed"],
        "failed_cells": len(failures),
    }, ensure_ascii=False, sort_keys=True), flush=True)
    return _process_exit_code(summary, failures)


if __name__ == "__main__":
    raise SystemExit(main())
