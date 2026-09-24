"""Run the resumable 100-case, 1--5 core Scene-B baseline experiment.

Each case is processed in ascending core count.  For target core counts 3--5,
the official Scene-B winners from every completed lower count starting at two
cores are padded with empty schedules and evaluated as separate ablations.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from candidate_manager_problem1 import sha256_file
from candidate_manager_problem2 import (
    ORIGINAL_CANDIDATES,
    InheritedCandidateSource,
    json_sha256,
    official_scene_b_hash,
    problem2_implementation_hash,
    run_problem2_candidate_group,
)


FULL_CORE_COUNTS = (1, 2, 3, 4, 5)
FULL_CASE_COUNT = 100
CASE_FILENAME_PATTERN = re.compile(r"^case_(\d{3})\.json$")


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
    path: Path, rows: Iterable[Mapping[str, Any]], fields: Sequence[str],
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    temporary.replace(path)


def _git_head(repo: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _candidate_names(cores: int) -> List[str]:
    names = list(ORIGINAL_CANDIDATES)
    if cores >= 3:
        names.extend("inherit_k{}".format(source) for source in range(2, cores))
    return names


def _load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object required: {}".format(path))
    return value


def _current_identity(repo: Path, graph_path: Path) -> Dict[str, str]:
    evaluator_hash, _ = official_scene_b_hash()
    implementation_hash, _ = problem2_implementation_hash()
    return {
        "graph_sha256": sha256_file(graph_path),
        "config_sha256": sha256_file(repo / "data" / "config.txt"),
        "solver_sha256": sha256_file(repo / "solver_problem1.py"),
        "evaluator_sha256": evaluator_hash,
        "problem2_implementation_sha256": implementation_hash,
    }


def _group_is_reusable(
    path: Path,
    repo: Path,
    graph_path: Path,
    cores: int,
    inherited: Sequence[InheritedCandidateSource],
) -> bool:
    if not path.is_file():
        return False
    try:
        group = _load_json(path)
        if group.get("status") != "success":
            return False
        if group.get("case") != graph_path.stem or group.get("cores") != cores:
            return False
        identity = _current_identity(repo, graph_path)
        if any(group.get(name) != value for name, value in identity.items()):
            return False
        if group.get("candidate_priority") != _candidate_names(cores):
            return False
        by_name = {
            item.get("name"): item for item in group.get("candidates", [])
            if isinstance(item, Mapping)
        }
        for source in inherited:
            item = by_name.get("inherit_k{}".format(source.source_cores))
            if not item:
                return False
            parameters = item.get("parameters") or {}
            if parameters.get("source_group_hash") != source.source_group_hash:
                return False
        return _group_artifacts_intact(group, path, cores)
    except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
        return False


def _group_artifacts_intact(
    group: Mapping[str, Any], group_path: Path, cores: int,
) -> bool:
    """Verify every artifact needed for a fully traceable resumed group."""

    run_dir = group_path.parent / "k{}".format(cores)
    final_plan = run_dir / "final_multicore_res.json"
    final_result = run_dir / "final_official_evaluation.json"
    if (not final_plan.is_file()
            or sha256_file(final_plan) != group.get("final_plan_file_sha256")):
        return False
    if (not final_result.is_file()
            or sha256_file(final_result)
            != group.get("final_official_result_file_sha256")):
        return False
    try:
        if json_sha256(_load_json(final_result)) != group.get(
                "final_official_result_json_sha256"):
            return False
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False

    record_hashes = group.get("candidate_record_file_sha256")
    candidates = group.get("candidates")
    if not isinstance(record_hashes, Mapping) or not isinstance(candidates, list):
        return False
    if set(record_hashes) != {item.get("name") for item in candidates}:
        return False
    for candidate in candidates:
        name = candidate.get("name")
        if not isinstance(name, str):
            return False
        record_path = run_dir / "candidate_records" / (name + ".json")
        plan_path = run_dir / "plans" / (name + "_multicore_res.json")
        if (not record_path.is_file()
                or sha256_file(record_path) != record_hashes.get(name)):
            return False
        if (not plan_path.is_file()
                or sha256_file(plan_path) != candidate.get("plan_file_sha256")):
            return False
        if candidate.get("status") == "evaluated":
            canonical = candidate.get("canonical_candidate")
            result_path = run_dir / "official_results" / (str(canonical) + ".json")
            if (not result_path.is_file()
                    or sha256_file(result_path)
                    != candidate.get("official_result_file_sha256")):
                return False
            try:
                if json_sha256(_load_json(result_path)) != candidate.get(
                        "official_result_json_sha256"):
                    return False
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return False
    return True


def _source_from_group(
    output: Path, case: str, source_cores: int,
) -> InheritedCandidateSource | None:
    group_path = output / "groups" / case / "k{}.json".format(source_cores)
    plan_path = (
        output / "groups" / case / "k{}".format(source_cores)
        / "final_multicore_res.json")
    if not group_path.is_file() or not plan_path.is_file():
        return None
    group = _load_json(group_path)
    winner = group.get("winner")
    if group.get("status") != "success" or not isinstance(winner, Mapping):
        return None
    return InheritedCandidateSource(
        source_cores=source_cores,
        source_plan=_load_json(plan_path),
        source_winner=str(winner["name"]),
        source_plan_path=str(plan_path.resolve()),
        source_group_hash=sha256_file(group_path),
    )


def _run_case(payload: Mapping[str, Any]) -> Dict[str, Any]:
    repo = Path(payload["repo"])
    output = Path(payload["output"])
    cache = Path(payload["cache"])
    case = str(payload["case"])
    max_cores = int(payload["max_cores"])
    graph_path = repo / "data" / (case + ".json")
    config_path = repo / "data" / "config.txt"
    records: List[Dict[str, Any]] = []
    for cores in range(1, max_cores + 1):
        inherited = [
            source
            for source_core in range(2, cores)
            for source in [_source_from_group(output, case, source_core)]
            if source is not None
        ]
        group_path = output / "groups" / case / "k{}.json".format(cores)
        started = time.perf_counter()
        try:
            if _group_is_reusable(
                    group_path, repo, graph_path, cores, inherited):
                group = _load_json(group_path)
                disposition = "resumed_valid_group"
            else:
                result = run_problem2_candidate_group(
                    graph_path,
                    cores,
                    config_path=config_path,
                    output_root=output,
                    cache_dir=cache,
                    inherited_sources=inherited,
                )
                group = result.manifest
                disposition = "executed"
            records.append({
                "case": case,
                "cores": cores,
                "status": "success",
                "run_disposition": disposition,
                "winner": (group.get("winner") or {}).get("name"),
                "wall_seconds": time.perf_counter() - started,
                "error": None,
            })
        except Exception as error:
            records.append({
                "case": case,
                "cores": cores,
                "status": "failed",
                "run_disposition": "failed",
                "winner": None,
                "wall_seconds": time.perf_counter() - started,
                "error": "{}: {}".format(type(error).__name__, error),
            })
    return {"case": case, "groups": records}


def _candidate_rows(groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in groups:
        for candidate in group.get("candidates", []):
            metrics = candidate.get("metrics") or {}
            timing = candidate.get("timing") or {}
            rows.append({
                "case": group["case"],
                "cores": group["cores"],
                "candidate": candidate.get("name"),
                "family": candidate.get("family"),
                "provenance": candidate.get("provenance"),
                "status": candidate.get("status"),
                "legal": candidate.get("legal"),
                "failure_stage": candidate.get("failure_stage"),
                "error_type": candidate.get("error_type"),
                "error": candidate.get("error"),
                "deduplicated": candidate.get("deduplicated"),
                "canonical_candidate": candidate.get("canonical_candidate"),
                "canonical_signature": candidate.get("canonical_signature"),
                "plan_sha256": candidate.get("plan_hash"),
                "plan_file_sha256": candidate.get("plan_file_sha256"),
                "cache_hit": candidate.get("cache_hit"),
                "evaluation_key": candidate.get("evaluation_key"),
                "makespan_cycles": metrics.get("makespan_cycles"),
                "original_graph_copy_bytes": metrics.get(
                    "original_graph_copy_bytes"),
                "scheduled_copy_bytes": metrics.get("scheduled_copy_bytes"),
                "added_copy_bytes": metrics.get("added_copy_bytes"),
                "partition_added_copy_bytes": metrics.get(
                    "partition_added_copy_bytes"),
                "spill_added_copy_bytes": metrics.get("spill_added_copy_bytes"),
                "cross_task_traffic_bytes": metrics.get(
                    "cross_task_traffic_bytes"),
                "cross_core_transfer_count": metrics.get(
                    "cross_core_transfer_count"),
                "used_core_count": metrics.get("used_core_count"),
                "generation_seconds": timing.get("generation_seconds"),
                "validation_seconds": timing.get("validation_seconds"),
                "official_evaluation_seconds": timing.get(
                    "official_evaluation_seconds"),
                "cold_cache_estimated_seconds": timing.get(
                    "cold_cache_estimated_seconds"),
                "invocation_wall_seconds": timing.get(
                    "invocation_wall_seconds"),
                "graph_sha256": group.get("graph_sha256"),
                "config_sha256": group.get("config_sha256"),
                "solver_sha256": group.get("solver_sha256"),
                "evaluator_sha256": group.get("evaluator_sha256"),
                "problem2_implementation_sha256": group.get(
                    "problem2_implementation_sha256"),
            })
    return rows


def _utilization_rows(groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in groups:
        for candidate in group.get("candidates", []):
            metrics = candidate.get("metrics") or {}
            for core in metrics.get("per_core_utilization", []):
                rows.append({
                    "case": group["case"],
                    "cores": group["cores"],
                    "candidate": candidate.get("name"),
                    "status": candidate.get("status"),
                    "core_id": core.get("core_id"),
                    "op_count": core.get("op_count"),
                    "task_start": core.get("task_start"),
                    "task_end": core.get("task_end"),
                    "task_span_cycles": core.get("task_span_cycles"),
                    "active_any_pipe_cycles": core.get("active_any_pipe_cycles"),
                    "active_any_pipe_fraction": core.get(
                        "active_any_pipe_fraction"),
                    "pipe_busy_cycles_json": json.dumps(
                        core.get("pipe_busy_cycles", {}), sort_keys=True),
                    "pipe_utilization_json": json.dumps(
                        core.get("pipe_utilization", {}), sort_keys=True),
                })
    return rows


def _winner_rows(groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    by_case_core = {(group["case"], group["cores"]): group for group in groups}
    baseline: Dict[str, int] = {}
    for group in groups:
        if group["cores"] != 1:
            continue
        single = next((
            candidate for candidate in group.get("candidates", [])
            if candidate.get("name") == "single"
            and candidate.get("status") == "evaluated"
        ), None)
        if single:
            baseline[group["case"]] = int(
                single["metrics"]["makespan_cycles"])
    rows: List[Dict[str, Any]] = []
    for case, cores in sorted(by_case_core):
        group = by_case_core[(case, cores)]
        winner = group.get("winner") or {}
        metrics = winner.get("metrics") or {}
        makespan = metrics.get("makespan_cycles")
        base = baseline.get(case)
        rows.append({
            "case": case,
            "cores": cores,
            "status": group.get("status"),
            "winner": winner.get("name"),
            "single_core_makespan_cycles": base,
            "makespan_cycles": makespan,
            "speedup": (base / makespan if base is not None and makespan else None),
            "added_copy_bytes": metrics.get("added_copy_bytes"),
            "partition_added_copy_bytes": metrics.get(
                "partition_added_copy_bytes"),
            "spill_added_copy_bytes": metrics.get("spill_added_copy_bytes"),
            "cross_task_traffic_bytes": metrics.get("cross_task_traffic_bytes"),
            "cross_core_transfer_count": metrics.get(
                "cross_core_transfer_count"),
            "used_core_count": metrics.get("used_core_count"),
            "group_wall_seconds": (group.get("timing") or {}).get(
                "invocation_wall_seconds"),
            "group_cold_cache_estimated_seconds": (group.get("timing") or {}).get(
                "cold_cache_estimated_seconds"),
        })
    return rows


def _paired_rows(candidate_rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    singles = {
        (row["case"], row["cores"]): row
        for row in candidate_rows
        if row["candidate"] == "single" and row["status"] == "evaluated"
    }
    rows: List[Dict[str, Any]] = []
    for row in candidate_rows:
        base = singles.get((row["case"], row["cores"]))
        if base is None or row["status"] != "evaluated":
            continue
        base_time = int(base["makespan_cycles"])
        current = int(row["makespan_cycles"])
        rows.append({
            "case": row["case"],
            "cores": row["cores"],
            "candidate": row["candidate"],
            "single_makespan_cycles": base_time,
            "candidate_makespan_cycles": current,
            "speedup_vs_single": base_time / current if current else None,
            "makespan_delta_cycles": current - base_time,
            "added_copy_delta_bytes": (
                int(row["added_copy_bytes"]) - int(base["added_copy_bytes"])),
            "spill_delta_bytes": (
                int(row["spill_added_copy_bytes"])
                - int(base["spill_added_copy_bytes"])),
            "cross_payload_delta_bytes": (
                int(row["cross_task_traffic_bytes"])
                - int(base["cross_task_traffic_bytes"])),
        })
    return rows


def _aggregate_winners(
    winner_rows: Sequence[Mapping[str, Any]], expected_cases: int,
    core_counts: Sequence[int],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for cores in core_counts:
        group = [row for row in winner_rows if row["cores"] == cores]
        successful = [row for row in group if row.get("speedup") is not None]
        speedups = [float(row["speedup"]) for row in successful]
        makespans = [float(row["makespan_cycles"]) for row in successful]
        rows.append({
            "cores": cores,
            "expected_cases": expected_cases,
            "observed_groups": len(group),
            "success_count": len(successful),
            "failure_count": expected_cases - len(successful),
            "failure_rate": (
                (expected_cases - len(successful)) / expected_cases
                if expected_cases else 0.0),
            "speedup_mean": statistics.fmean(speedups) if speedups else None,
            "speedup_median": statistics.median(speedups) if speedups else None,
            "speedup_p05": _quantile(speedups, 0.05),
            "speedup_p10": _quantile(speedups, 0.10),
            "speedup_min": min(speedups) if speedups else None,
            "makespan_median": statistics.median(makespans) if makespans else None,
            "makespan_p90": _quantile(makespans, 0.90),
            "makespan_p95": _quantile(makespans, 0.95),
            "makespan_p99": _quantile(makespans, 0.99),
            "makespan_max": max(makespans) if makespans else None,
        })
    return rows


def _aggregate_candidates(
    candidate_rows: Sequence[Mapping[str, Any]], expected_cases: int,
) -> List[Dict[str, Any]]:
    keys = sorted({
        (int(row["cores"]), str(row["candidate"])) for row in candidate_rows
    })
    rows: List[Dict[str, Any]] = []
    for cores, candidate in keys:
        group = [row for row in candidate_rows
                 if row["cores"] == cores and row["candidate"] == candidate]
        success = [row for row in group if row["status"] == "evaluated"]
        makespans = [float(row["makespan_cycles"]) for row in success]
        rows.append({
            "cores": cores,
            "candidate": candidate,
            "expected_cases": expected_cases,
            "record_count": len(group),
            "success_count": len(success),
            "failure_count": expected_cases - len(success),
            "failure_rate": (
                (expected_cases - len(success)) / expected_cases
                if expected_cases else 0.0),
            "makespan_mean": statistics.fmean(makespans) if makespans else None,
            "makespan_median": statistics.median(makespans) if makespans else None,
            "makespan_p90": _quantile(makespans, 0.90),
            "makespan_p95": _quantile(makespans, 0.95),
            "makespan_p99": _quantile(makespans, 0.99),
            "makespan_max": max(makespans) if makespans else None,
            "added_copy_mean": (
                statistics.fmean(float(row["added_copy_bytes"]) for row in success)
                if success else None),
            "spill_mean": (
                statistics.fmean(
                    float(row["spill_added_copy_bytes"]) for row in success)
                if success else None),
            "cross_payload_mean": (
                statistics.fmean(
                    float(row["cross_task_traffic_bytes"]) for row in success)
                if success else None),
        })
    return rows


def summarize(output: Path, cases: Sequence[str], max_cores: int) -> Dict[str, Any]:
    groups: List[Dict[str, Any]] = []
    for case in sorted(cases):
        for cores in range(1, max_cores + 1):
            path = output / "groups" / case / "k{}.json".format(cores)
            if path.is_file():
                groups.append(_load_json(path))
    candidate_rows = _candidate_rows(groups)
    utilization_rows = _utilization_rows(groups)
    winner_rows = _winner_rows(groups)
    paired_rows = _paired_rows(candidate_rows)
    winner_aggregate = _aggregate_winners(
        winner_rows, len(cases), tuple(range(1, max_cores + 1)))
    candidate_aggregate = _aggregate_candidates(candidate_rows, len(cases))
    _write_csv(output / "candidate_results.csv", candidate_rows, (
        "case", "cores", "candidate", "family", "provenance", "status",
        "legal", "failure_stage", "error_type", "error", "deduplicated",
        "canonical_candidate", "canonical_signature", "plan_sha256",
        "plan_file_sha256", "cache_hit", "evaluation_key", "makespan_cycles",
        "original_graph_copy_bytes", "scheduled_copy_bytes", "added_copy_bytes",
        "partition_added_copy_bytes", "spill_added_copy_bytes",
        "cross_task_traffic_bytes", "cross_core_transfer_count",
        "used_core_count", "generation_seconds", "validation_seconds",
        "official_evaluation_seconds", "cold_cache_estimated_seconds",
        "invocation_wall_seconds", "graph_sha256", "config_sha256",
        "solver_sha256", "evaluator_sha256",
        "problem2_implementation_sha256",
    ))
    _write_csv(output / "per_core_utilization.csv", utilization_rows, (
        "case", "cores", "candidate", "status", "core_id", "op_count",
        "task_start", "task_end", "task_span_cycles",
        "active_any_pipe_cycles", "active_any_pipe_fraction",
        "pipe_busy_cycles_json", "pipe_utilization_json",
    ))
    _write_csv(output / "selected_results.csv", winner_rows, (
        "case", "cores", "status", "winner", "single_core_makespan_cycles",
        "makespan_cycles", "speedup", "added_copy_bytes",
        "partition_added_copy_bytes", "spill_added_copy_bytes",
        "cross_task_traffic_bytes", "cross_core_transfer_count",
        "used_core_count", "group_wall_seconds",
        "group_cold_cache_estimated_seconds",
    ))
    _write_csv(output / "paired_vs_single.csv", paired_rows, (
        "case", "cores", "candidate", "single_makespan_cycles",
        "candidate_makespan_cycles", "speedup_vs_single",
        "makespan_delta_cycles", "added_copy_delta_bytes", "spill_delta_bytes",
        "cross_payload_delta_bytes",
    ))
    _write_csv(output / "aggregate_by_core.csv", winner_aggregate,
               tuple(winner_aggregate[0]) if winner_aggregate else ())
    _write_csv(output / "candidate_aggregate.csv", candidate_aggregate,
               tuple(candidate_aggregate[0]) if candidate_aggregate else ())
    failures = [row for row in candidate_rows if row["status"] != "evaluated"]
    _write_csv(output / "failures.csv", failures, (
        "case", "cores", "candidate", "status", "legal", "failure_stage",
        "error_type", "error", "graph_sha256", "config_sha256",
        "solver_sha256", "evaluator_sha256",
        "problem2_implementation_sha256",
    ))

    representative_rows = [
        row for row in candidate_rows
        if row["status"] == "evaluated" and not row["deduplicated"]
    ]
    observed_cold = [
        float(row["cold_cache_estimated_seconds"])
        for row in representative_rows
        if row.get("cold_cache_estimated_seconds") is not None
    ]
    logical_per_case = sum(
        len(_candidate_names(cores)) for cores in FULL_CORE_COUNTS)
    budget = {
        "full_case_count": FULL_CASE_COUNT,
        "core_counts": list(FULL_CORE_COUNTS),
        "groups": FULL_CASE_COUNT * len(FULL_CORE_COUNTS),
        "logical_candidate_attempts": FULL_CASE_COUNT * logical_per_case,
        "logical_candidates_per_case": logical_per_case,
        "maximum_unique_official_evaluations": FULL_CASE_COUNT * logical_per_case,
        "observed_unique_representatives": len(representative_rows),
        "observed_cold_seconds_mean": (
            statistics.fmean(observed_cold) if observed_cold else None),
        "observed_cold_seconds_median": (
            statistics.median(observed_cold) if observed_cold else None),
        "observed_cold_seconds_p95": _quantile(observed_cold, 0.95),
        "projected_single_worker_seconds_using_observed_mean": (
            statistics.fmean(observed_cold) * FULL_CASE_COUNT * logical_per_case
            if observed_cold else None),
        "note": (
            "Upper bound assumes no deduplication; wall time under multiple workers "
            "is not claimed to scale linearly because evaluations share CPU and disk."),
    }
    _write_json(output / "budget_estimate.json", budget)
    summary = {
        "schema_version": 1,
        "experiment": "problem2_scene_b_baseline",
        "requested_cases": list(cases),
        "requested_max_cores": max_cores,
        "expected_groups": len(cases) * max_cores,
        "recorded_groups": len(groups),
        "successful_groups": sum(group.get("status") == "success" for group in groups),
        "candidate_record_count": len(candidate_rows),
        "candidate_failure_count": len(failures),
        "budget": budget,
        "timestamp": _utc_now(),
    }
    _write_json(output / "summary.json", summary)
    return summary


def _discover_cases(repo: Path) -> List[str]:
    cases: List[str] = []
    for path in (repo / "data").glob("case_*.json"):
        match = CASE_FILENAME_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        case_number = int(match.group(1))
        if 1 <= case_number <= FULL_CASE_COUNT:
            cases.append(path.stem)
    return sorted(cases)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--cases", nargs="+", help="case names; default: all cases")
    parser.add_argument("--max-cores", type=int, default=5, choices=FULL_CORE_COUNTS)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    repo = args.repo.resolve()
    output = args.output.resolve()
    cache = (args.cache or output / "cache").resolve()
    cases = sorted(args.cases or _discover_cases(repo))
    if not cases:
        parser.error("no case files found")
    missing = [case for case in cases
               if not (repo / "data" / (case + ".json")).is_file()]
    if missing:
        parser.error("case files not found: {}".format(", ".join(missing)))
    if args.workers < 1:
        parser.error("--workers must be positive")

    evaluator_hash, evaluator_files = official_scene_b_hash()
    implementation_hash, implementation_files = problem2_implementation_hash()
    run_identity = {
        "schema_version": 1,
        "experiment": "problem2_scene_b_baseline",
        "git_head": _git_head(repo),
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "solver_sha256": sha256_file(repo / "solver_problem1.py"),
        "config_sha256": sha256_file(repo / "data" / "config.txt"),
        "evaluator_sha256": evaluator_hash,
        "evaluator_file_sha256": evaluator_files,
        "problem2_implementation_sha256": implementation_hash,
        "implementation_file_sha256": implementation_files,
        "cases": cases,
        "max_cores": args.max_cores,
        "workers": args.workers,
        "inheritance_policy": {
            "source": "official Scene-B winner at each lower core count",
            "minimum_source_cores": 2,
            "padding": "append empty core schedules only",
            "one_core_inheritance": "omitted because target single is equivalent",
        },
        "started_at": _utc_now(),
    }
    _write_json(output / "run_identity.json", run_identity)

    payloads = [{
        "repo": str(repo),
        "output": str(output),
        "cache": str(cache),
        "case": case,
        "max_cores": args.max_cores,
    } for case in cases]
    progress: List[Dict[str, Any]] = []
    if args.workers == 1:
        for payload in payloads:
            result = _run_case(payload)
            progress.extend(result["groups"])
            print("{}: {}".format(result["case"], result["groups"][-1]["status"]),
                  flush=True)
            summarize(output, cases, args.max_cores)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(_run_case, payload): payload["case"]
                       for payload in payloads}
            for future in as_completed(futures):
                case = futures[future]
                try:
                    result = future.result()
                    progress.extend(result["groups"])
                    print("{}: {}".format(
                        case, result["groups"][-1]["status"]), flush=True)
                except Exception as error:
                    progress.append({
                        "case": case,
                        "cores": None,
                        "status": "worker_failed",
                        "run_disposition": "failed",
                        "winner": None,
                        "wall_seconds": None,
                        "error": "{}: {}".format(type(error).__name__, error),
                    })
                    print("{}: worker_failed: {}".format(case, error), flush=True)
                _write_json(output / "latest_progress.json", {
                    "records": sorted(progress, key=lambda item: (
                        item["case"], item["cores"] or 0)),
                    "timestamp": _utc_now(),
                })
                summarize(output, cases, args.max_cores)
    _write_json(output / "latest_progress.json", {
        "records": sorted(progress, key=lambda item: (
            item["case"], item["cores"] or 0)),
        "timestamp": _utc_now(),
    })
    summary = summarize(output, cases, args.max_cores)
    failures = [item for item in progress if item["status"] != "success"]
    complete = (
        summary["recorded_groups"] == summary["expected_groups"]
        and summary["successful_groups"] == summary["expected_groups"]
        and not failures
    )
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
