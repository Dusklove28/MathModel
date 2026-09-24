"""Resumable full-scale Problem-1 experiment orchestration.

This runner does not define or modify scheduling candidates.  It reuses the
locked six-candidate manager for k=2,3,4,5 and evaluates one explicit Single
plan for the official one-core baseline.  Every successful official result is
stored once in the existing content-addressed cache and referenced from the
per-group record.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from candidate_manager_problem1 import (
    CANDIDATE_PRIORITY,
    CandidateManagerError,
    EvaluationCache,
    _validate_official_result,
    canonical_plan_signature,
    evaluate_official_problem1,
    evaluation_key,
    official_evaluator_hash,
    run_candidate_manager,
    sha256_file,
)
from contest_io import _read_json
from evaluation_validation import validate_task_order
from solver_problem1 import (
    GraphInfo,
    generate_single_plan_from_graph_info,
)
from stub_multicore_cut_and_schedule import derive_multicore_plan


DEFAULT_CORES = (2, 3, 4, 5)


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


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    temporary.replace(path)


def _git_commit(project_dir: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=project_dir,
            text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _host_resources() -> Dict[str, Any]:
    resources: Dict[str, Any] = {
        "logical_cpu_count": os.cpu_count(),
        "physical_cpu_count": None,
        "total_memory_bytes": None,
        "available_memory_bytes": None,
    }
    try:
        import psutil

        memory = psutil.virtual_memory()
        resources.update({
            "physical_cpu_count": psutil.cpu_count(logical=False),
            "total_memory_bytes": memory.total,
            "available_memory_bytes": memory.available,
        })
    except (ImportError, OSError):
        pass
    return resources


class _PeakRssSampler:
    def __init__(self, interval: float = 0.1):
        self.interval = interval
        self.peak: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_PeakRssSampler":
        try:
            import psutil
        except ImportError:
            return self
        process = psutil.Process(os.getpid())

        def sample() -> None:
            while not self._stop.is_set():
                try:
                    processes = [process, *process.children(recursive=True)]
                    rss = sum(item.memory_info().rss for item in processes if item.is_running())
                    self.peak = rss if self.peak is None else max(self.peak, rss)
                except (psutil.Error, OSError):
                    pass
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval * 3))


def _fingerprint(project_dir: Path, config_path: Path) -> Dict[str, Any]:
    evaluator_hash, evaluator_files = official_evaluator_hash()
    return {
        "solver_sha256": sha256_file(project_dir / "solver_problem1.py"),
        "candidate_manager_sha256": sha256_file(
            project_dir / "candidate_manager_problem1.py"),
        "config_sha256": sha256_file(config_path),
        "evaluator_sha256": evaluator_hash,
        "evaluator_file_sha256": evaluator_files,
        "candidate_priority": list(CANDIDATE_PRIORITY),
        "git_commit": _git_commit(project_dir),
    }


def _load_development_scores(path: Path | None) -> Dict[Tuple[str, int], Dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    summary = _read_json(path)
    return {
        (row["case"], int(row["cores"])): dict(row)
        for row in summary.get("results", [])
    }


def _manifest_matches_development(
    manifest: Mapping[str, Any], development_row: Mapping[str, Any],
) -> bool:
    winner = manifest.get("winner") or {}
    return (
        manifest.get("case") == development_row.get("case")
        and manifest.get("cores") == development_row.get("cores")
        and tuple(manifest.get("candidate_priority", [])) == CANDIDATE_PRIORITY
        and winner.get("name") == development_row.get("winner")
        and winner.get("makespan") == development_row.get("winner_makespan")
        and winner.get("added_copy_bytes") == development_row.get("winner_added_bytes")
        and manifest.get("fatal_error") is None
    )


def _candidate_group_record_path(full_root: Path, case: str, cores: int) -> Path:
    return full_root / "groups" / case / ("k{}.json".format(cores))


def _baseline_record_path(full_root: Path, case: str) -> Path:
    return full_root / "single_baselines" / case / "record.json"


def _cache_entry(cache_dir: Path, key: str | None) -> Mapping[str, Any] | None:
    if not key:
        return None
    path = cache_dir / "entries" / (key + ".json")
    if not path.is_file():
        return None
    try:
        entry = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return entry if isinstance(entry, Mapping) else None


def _candidate_timing(
    manifest: Mapping[str, Any], cache_dir: Path,
) -> Dict[str, Any]:
    unique: Dict[str, Mapping[str, Any]] = {}
    for candidate in manifest.get("candidates", []):
        if candidate.get("status") == "evaluated" and candidate.get("evaluation_key"):
            unique.setdefault(candidate["evaluation_key"], candidate)
    cold_times: List[float] = []
    missing: List[str] = []
    for key in unique:
        entry = _cache_entry(cache_dir, key)
        if entry is None or not isinstance(entry.get("evaluation_time"), (int, float)):
            missing.append(key)
        else:
            cold_times.append(float(entry["evaluation_time"]))
    timing = manifest.get("timing", {})
    recorded_wall = float(timing.get("total_wall_time", 0.0))
    recorded_eval = float(timing.get("evaluation_time", 0.0))
    cold_eval = sum(cold_times) if not missing else None
    cold_wall = (
        max(0.0, recorded_wall - recorded_eval) + cold_eval
        if cold_eval is not None else None
    )
    return {
        "unique_evaluated_plans": len(unique),
        "actual_candidate_manager_wall_time_seconds": recorded_wall,
        "actual_official_evaluation_time_seconds": recorded_eval,
        "cold_cache_official_evaluation_time_seconds": cold_eval,
        "cold_cache_estimated_wall_time_seconds": cold_wall,
        "cold_cache_timing_complete": not missing,
        "missing_cache_timing_keys": missing,
    }


def _successful_group_record(
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    final_plan_path: Path,
    cache_dir: Path,
    fingerprint: Mapping[str, Any],
    graph_hash: str,
    disposition: str,
    observed_wall: float,
    peak_rss: int | None,
) -> Dict[str, Any]:
    winner = manifest["winner"]
    return {
        "schema_version": 1,
        "kind": "candidate_group",
        "status": "success",
        "run_disposition": disposition,
        "case": manifest["case"],
        "cores": manifest["cores"],
        "graph_sha256": graph_hash,
        "fingerprint": dict(fingerprint),
        "winner": winner,
        "generated_candidates": manifest.get("generated_candidates"),
        "unique_plans": manifest.get("unique_plans"),
        "official_evaluations": manifest.get("official_evaluations"),
        "cache_hits": manifest.get("cache_hits"),
        "failed_candidates": list(manifest.get("failed_candidates", [])),
        "candidates": list(manifest.get("candidates", [])),
        "manifest_path": str(manifest_path.resolve()),
        "final_plan_path": str(final_plan_path.resolve()),
        "timing": {
            **_candidate_timing(manifest, cache_dir),
            "current_invocation_observed_wall_time_seconds": observed_wall,
        },
        "peak_worker_rss_bytes": peak_rss,
        "timestamp": _utc_now(),
    }


def _run_candidate_group(task: Mapping[str, Any]) -> Dict[str, Any]:
    started = time.perf_counter()
    project_dir = Path(task["project_dir"])
    graph_path = Path(task["graph_path"])
    config_path = Path(task["config_path"])
    candidate_root = Path(task["candidate_root"])
    cache_dir = Path(task["cache_dir"])
    full_root = Path(task["full_root"])
    cores = int(task["cores"])
    fingerprint = task["fingerprint"]
    graph_hash = sha256_file(graph_path)
    record_path = _candidate_group_record_path(full_root, graph_path.stem, cores)
    manifest_path = candidate_root / graph_path.stem / ("k{}".format(cores)) / "manifest.json"
    final_plan_path = manifest_path.parent / "final_multicore_res.json"

    with _PeakRssSampler() as sampler:
        try:
            if record_path.is_file():
                existing = _read_json(record_path)
                if (
                    existing.get("status") == "success"
                    and existing.get("graph_sha256") == graph_hash
                    and existing.get("fingerprint") == fingerprint
                    and Path(existing["manifest_path"]).is_file()
                    and Path(existing["final_plan_path"]).is_file()
                ):
                    existing = dict(existing)
                    existing["run_disposition"] = "resumed_group_record"
                    existing["timing"] = dict(existing.get("timing", {}))
                    existing["timing"]["current_invocation_observed_wall_time_seconds"] = (
                        time.perf_counter() - started)
                    return existing

            development_row = task.get("development_row")
            if development_row and manifest_path.is_file() and final_plan_path.is_file():
                manifest = _read_json(manifest_path)
                if (
                    _manifest_matches_development(manifest, development_row)
                    and manifest.get("graph_hash") == graph_hash
                    and manifest.get("config_hash") == fingerprint["config_sha256"]
                    and manifest.get("evaluator_hash") == fingerprint["evaluator_sha256"]
                ):
                    record = _successful_group_record(
                        manifest=manifest,
                        manifest_path=manifest_path,
                        final_plan_path=final_plan_path,
                        cache_dir=cache_dir,
                        fingerprint=fingerprint,
                        graph_hash=graph_hash,
                        disposition="adopted_locked_development_manifest",
                        observed_wall=time.perf_counter() - started,
                        peak_rss=sampler.peak,
                    )
                    _write_json(record_path, record)
                    return record

            result = run_candidate_manager(
                graph_path, cores, config_path=config_path,
                output_root=candidate_root, cache_dir=cache_dir)
            record = _successful_group_record(
                manifest=result.manifest,
                manifest_path=result.manifest_path,
                final_plan_path=result.final_plan_path,
                cache_dir=cache_dir,
                fingerprint=fingerprint,
                graph_hash=graph_hash,
                disposition="executed",
                observed_wall=time.perf_counter() - started,
                peak_rss=sampler.peak,
            )
            _write_json(record_path, record)
            return record
        except Exception as error:
            record = {
                "schema_version": 1,
                "kind": "candidate_group",
                "status": "failed",
                "run_disposition": "failed",
                "case": graph_path.stem,
                "cores": cores,
                "graph_sha256": graph_hash,
                "fingerprint": dict(fingerprint),
                "error_type": type(error).__name__,
                "error": str(error),
                "timing": {
                    "current_invocation_observed_wall_time_seconds": (
                        time.perf_counter() - started),
                },
                "peak_worker_rss_bytes": sampler.peak,
                "timestamp": _utc_now(),
            }
            _write_json(record_path, record)
            return record


def _run_single_baseline(task: Mapping[str, Any]) -> Dict[str, Any]:
    started = time.perf_counter()
    graph_path = Path(task["graph_path"])
    config_path = Path(task["config_path"])
    cache_dir = Path(task["cache_dir"])
    full_root = Path(task["full_root"])
    fingerprint = task["fingerprint"]
    graph_hash = sha256_file(graph_path)
    record_path = _baseline_record_path(full_root, graph_path.stem)
    run_dir = record_path.parent
    plan_path = run_dir / "single_core_multicore_res.json"

    with _PeakRssSampler() as sampler:
        try:
            if record_path.is_file():
                existing = _read_json(record_path)
                if (
                    existing.get("status") == "success"
                    and existing.get("graph_sha256") == graph_hash
                    and existing.get("fingerprint") == fingerprint
                    and Path(existing["plan_path"]).is_file()
                ):
                    existing = dict(existing)
                    existing["run_disposition"] = "resumed_group_record"
                    existing["current_invocation_observed_wall_time_seconds"] = (
                        time.perf_counter() - started)
                    return existing

            graph = _read_json(graph_path)
            graph_info = GraphInfo.from_graph(graph)
            plan = generate_single_plan_from_graph_info(graph_info, 1)
            view = derive_multicore_plan(graph, plan)
            validate_task_order(view)
            plan_hash = canonical_plan_signature(plan)
            evaluator_hash = fingerprint["evaluator_sha256"]
            config_hash = fingerprint["config_sha256"]
            key = evaluation_key(graph_hash, config_hash, plan_hash, evaluator_hash)
            cache = EvaluationCache(cache_dir)
            lookup_started = time.perf_counter()
            entry = cache.lookup(graph_hash, config_hash, plan_hash, evaluator_hash)
            lookup_time = time.perf_counter() - lookup_started
            evaluation_time = 0.0
            cache_hit = entry is not None
            if entry is None:
                evaluation_started = time.perf_counter()
                result = evaluate_official_problem1(graph, plan, config_path)
                evaluation_time = time.perf_counter() - evaluation_started
                makespan, added, max_memory = _validate_official_result(result)
                entry, _ = cache.store(
                    graph_hash=graph_hash,
                    config_hash=config_hash,
                    plan_hash=plan_hash,
                    evaluator_hash=evaluator_hash,
                    result=result,
                    evaluation_time=evaluation_time,
                    max_memory_bytes=max_memory,
                    metadata={
                        "graph": graph_path.name,
                        "candidate": "single_core_baseline",
                        "num_cores": 1,
                    },
                )
            else:
                makespan = int(entry["makespan"])
                added = int(entry["added_copy_bytes"])
                max_memory = int(entry.get("max_memory_bytes", 0))
            cold_evaluation_time = float(entry["evaluation_time"])
            result_path = cache.results_dir / (key + ".json")
            _write_json(plan_path, plan)
            record = {
                "schema_version": 1,
                "kind": "single_core_baseline",
                "status": "success",
                "run_disposition": "cache_hit" if cache_hit else "executed",
                "case": graph_path.stem,
                "cores": 1,
                "graph_sha256": graph_hash,
                "fingerprint": dict(fingerprint),
                "plan_sha256": plan_hash,
                "evaluation_key": key,
                "makespan": makespan,
                "added_copy_bytes": added,
                "max_memory_bytes": max_memory,
                "plan_path": str(plan_path.resolve()),
                "official_result_path": str(result_path.resolve()),
                "cache_hit": cache_hit,
                "actual_official_evaluation_time_seconds": evaluation_time,
                "cold_cache_official_evaluation_time_seconds": cold_evaluation_time,
                "cache_lookup_time_seconds": lookup_time,
                "actual_wall_time_seconds": time.perf_counter() - started,
                "peak_worker_rss_bytes": sampler.peak,
                "timestamp": _utc_now(),
            }
            _write_json(record_path, record)
            return record
        except Exception as error:
            record = {
                "schema_version": 1,
                "kind": "single_core_baseline",
                "status": "failed",
                "run_disposition": "failed",
                "case": graph_path.stem,
                "cores": 1,
                "graph_sha256": graph_hash,
                "fingerprint": dict(fingerprint),
                "error_type": type(error).__name__,
                "error": str(error),
                "actual_wall_time_seconds": time.perf_counter() - started,
                "peak_worker_rss_bytes": sampler.peak,
                "timestamp": _utc_now(),
            }
            _write_json(record_path, record)
            return record


def _worker(task: Mapping[str, Any]) -> Dict[str, Any]:
    if task["kind"] == "single_core_baseline":
        return _run_single_baseline(task)
    return _run_candidate_group(task)


def _selected_cases(data_dir: Path, requested: Sequence[str] | None) -> List[str]:
    available = {path.stem: path for path in data_dir.glob("case_*.json")}
    if requested:
        names = [Path(value).stem for value in requested]
        missing = [name for name in names if name not in available]
        if missing:
            raise FileNotFoundError("missing cases: {}".format(", ".join(missing)))
        return sorted(set(names))
    names = sorted(available)
    if len(names) != 100:
        raise ValueError(
            "full run requires exactly 100 case_*.json files; found {}".format(len(names)))
    return names


def _development_preflight(
    tasks: Sequence[Mapping[str, Any]],
    candidate_root: Path,
    development_scores: Mapping[Tuple[str, int], Mapping[str, Any]],
    fingerprint: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = []
    for task in tasks:
        if task["kind"] != "candidate_group":
            continue
        key = (Path(task["graph_path"]).stem, int(task["cores"]))
        row = development_scores.get(key)
        if row is None:
            continue
        path = candidate_root / key[0] / ("k{}".format(key[1])) / "manifest.json"
        if not path.is_file():
            failures.append({"case": key[0], "cores": key[1], "reason": "missing_manifest"})
            continue
        try:
            manifest = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures.append({
                "case": key[0], "cores": key[1],
                "reason": "invalid_manifest: {}".format(error),
            })
            continue
        if not _manifest_matches_development(manifest, row):
            failures.append({"case": key[0], "cores": key[1], "reason": "summary_mismatch"})
        elif manifest.get("config_hash") != fingerprint["config_sha256"]:
            failures.append({"case": key[0], "cores": key[1], "reason": "config_hash_mismatch"})
        elif manifest.get("evaluator_hash") != fingerprint["evaluator_sha256"]:
            failures.append({"case": key[0], "cores": key[1], "reason": "evaluator_hash_mismatch"})
    return failures


def _resource_recommendation(
    records: Sequence[Mapping[str, Any]], host: Mapping[str, Any], workers: int,
) -> Dict[str, Any]:
    peaks = [
        int(record["peak_worker_rss_bytes"])
        for record in records
        if isinstance(record.get("peak_worker_rss_bytes"), int)
    ]
    peak = max(peaks, default=None)
    total_memory = host.get("total_memory_bytes")
    physical = host.get("physical_cpu_count") or host.get("logical_cpu_count") or 1
    memory_bound = None
    if peak and total_memory:
        memory_bound = max(1, math.floor(0.60 * total_memory / peak))
    bound = min(int(physical), memory_bound if memory_bound is not None else workers, 8)
    if workers == 2:
        recommended = min(max(2, bound), 4)
    else:
        recommended = min(max(1, bound), workers)
    return {
        "observed_task_count": len(peaks),
        "max_peak_worker_rss_bytes": peak,
        "conservative_two_worker_peak_sum_bytes": (peak * 2 if peak else None),
        "memory_bound_workers_at_60_percent": memory_bound,
        "cpu_bound_workers": int(physical),
        "current_workers": workers,
        "recommended_next_workers": recommended,
        "rerun_memory_trial_before_exceeding_recommendation": True,
    }


def _summarize(
    *,
    cases: Sequence[str],
    cores: Sequence[int],
    full_root: Path,
    fingerprint: Mapping[str, Any],
    host: Mapping[str, Any],
    workers: int,
    invocation_wall: float,
) -> Dict[str, Any]:
    baselines: Dict[str, Dict[str, Any]] = {}
    groups: List[Dict[str, Any]] = []
    for case in cases:
        baseline_path = _baseline_record_path(full_root, case)
        if baseline_path.is_file():
            baselines[case] = _read_json(baseline_path)
        for core in cores:
            path = _candidate_group_record_path(full_root, case, core)
            if path.is_file():
                groups.append(_read_json(path))

    rows: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for group in sorted(groups, key=lambda item: (item["case"], item["cores"])):
        baseline = baselines.get(group["case"])
        success = group.get("status") == "success"
        baseline_success = baseline is not None and baseline.get("status") == "success"
        t1 = baseline.get("makespan") if baseline_success else None
        winner = group.get("winner") or {}
        tk = winner.get("makespan") if success else None
        speedup = t1 / tk if t1 is not None and tk not in (None, 0) else None
        timing = group.get("timing", {})
        failed_candidates = group.get("failed_candidates", [])
        rows.append({
            "case": group["case"],
            "cores": group["cores"],
            "status": group.get("status"),
            "winner": winner.get("name"),
            "t_i_1": t1,
            "t_i_k": tk,
            "speedup_t1_over_tk": speedup,
            "added_copy_bytes": winner.get("added_copy_bytes"),
            "unique_plans": group.get("unique_plans"),
            "official_evaluations": group.get("official_evaluations"),
            "cache_hits": group.get("cache_hits"),
            "failed_candidates": ";".join(failed_candidates),
            "run_disposition": group.get("run_disposition"),
            "actual_wall_time_seconds": timing.get(
                "actual_candidate_manager_wall_time_seconds"),
            "cold_cache_estimated_wall_time_seconds": timing.get(
                "cold_cache_estimated_wall_time_seconds"),
            "peak_worker_rss_bytes": group.get("peak_worker_rss_bytes"),
        })
        if not success:
            failures.append({
                "case": group["case"], "cores": group["cores"],
                "scope": "group", "item": "", "error": group.get("error", ""),
            })
        for candidate in group.get("candidates", []):
            if candidate.get("status") == "failed":
                failures.append({
                    "case": group["case"], "cores": group["cores"],
                    "scope": "candidate", "item": candidate.get("name"),
                    "error": candidate.get("error", ""),
                })
    for case, baseline in sorted(baselines.items()):
        if baseline.get("status") != "success":
            failures.append({
                "case": case, "cores": 1, "scope": "baseline", "item": "single",
                "error": baseline.get("error", ""),
            })

    aggregate_rows: List[Dict[str, Any]] = []
    for core in cores:
        core_rows = [row for row in rows if row["cores"] == core]
        speedups = [
            float(row["speedup_t1_over_tk"])
            for row in core_rows if row["speedup_t1_over_tk"] is not None
        ]
        added = [
            int(row["added_copy_bytes"])
            for row in core_rows if row["added_copy_bytes"] is not None
        ]
        aggregate_rows.append({
            "cores": core,
            "completed_cases": len(speedups),
            "arithmetic_mean_speedup": (
                sum(speedups) / len(speedups) if speedups else None),
            "median_speedup": (
                sorted(speedups)[len(speedups) // 2]
                if len(speedups) % 2 == 1 else
                (sorted(speedups)[len(speedups) // 2 - 1]
                 + sorted(speedups)[len(speedups) // 2]) / 2
                if speedups else None),
            "total_added_copy_bytes": sum(added),
            "mean_added_copy_bytes": (sum(added) / len(added) if added else None),
            "failed_group_count": sum(row["status"] != "success" for row in core_rows),
        })
    recommendation = _resource_recommendation(
        [*baselines.values(), *groups], host, workers)
    summary = {
        "schema_version": 1,
        "fingerprint": dict(fingerprint),
        "host": dict(host),
        "workers": workers,
        "selected_case_count": len(cases),
        "selected_cases": list(cases),
        "cores": list(cores),
        "baseline_record_count": len(baselines),
        "candidate_group_record_count": len(groups),
        "expected_baseline_count": len(cases),
        "expected_candidate_group_count": len(cases) * len(cores),
        "invocation_wall_time_seconds": invocation_wall,
        "resource_recommendation": recommendation,
        "aggregate_by_cores": aggregate_rows,
        "failure_count": len(failures),
        "failures": failures,
        "results": rows,
        "baselines": [baselines[name] for name in sorted(baselines)],
        "groups": groups,
        "timestamp": _utc_now(),
    }
    _write_json(full_root / "problem1_full_summary.json", summary)
    _write_csv(full_root / "problem1_full_results.csv", rows, (
        "case", "cores", "status", "winner", "t_i_1", "t_i_k",
        "speedup_t1_over_tk", "added_copy_bytes", "unique_plans",
        "official_evaluations", "cache_hits", "failed_candidates",
        "run_disposition", "actual_wall_time_seconds",
        "cold_cache_estimated_wall_time_seconds", "peak_worker_rss_bytes",
    ))
    _write_csv(full_root / "problem1_full_aggregate.csv", aggregate_rows, (
        "cores", "completed_cases", "arithmetic_mean_speedup",
        "median_speedup", "total_added_copy_bytes", "mean_added_copy_bytes",
        "failed_group_count",
    ))
    _write_csv(full_root / "problem1_full_failures.csv", failures, (
        "case", "cores", "scope", "item", "error",
    ))
    _write_json(full_root / "resource_recommendation.json", recommendation)
    return summary


def run_full_experiment(args: argparse.Namespace) -> Dict[str, Any]:
    invocation_started = time.perf_counter()
    project_dir = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir).resolve()
    config_path = Path(args.config).resolve()
    full_root = Path(args.output_root).resolve()
    candidate_root = Path(args.candidate_output_root).resolve()
    cache_dir = Path(args.cache_dir).resolve()
    full_root.mkdir(parents=True, exist_ok=True)
    cases = _selected_cases(data_dir, args.cases)
    cores = tuple(sorted(set(args.cores)))
    if any(core not in DEFAULT_CORES for core in cores):
        raise ValueError("cores must be selected from 2, 3, 4, 5")
    fingerprint = _fingerprint(project_dir, config_path)
    fingerprint_path = full_root / "frozen_fingerprint.json"
    if fingerprint_path.is_file():
        previous = _read_json(fingerprint_path)
        if previous.get("fingerprint") != fingerprint:
            raise RuntimeError(
                "frozen code/config fingerprint changed; choose a new --output-root")
    else:
        _write_json(fingerprint_path, {
            "schema_version": 1,
            "fingerprint": fingerprint,
            "recorded_at": _utc_now(),
        })

    development_summary = (
        Path(args.development_summary).resolve()
        if args.development_summary else None)
    development_scores = _load_development_scores(development_summary)
    common = {
        "project_dir": str(project_dir),
        "config_path": str(config_path),
        "candidate_root": str(candidate_root),
        "cache_dir": str(cache_dir),
        "full_root": str(full_root),
        "fingerprint": fingerprint,
    }
    tasks: List[Dict[str, Any]] = []
    if not args.skip_single_baseline:
        for case in cases:
            tasks.append({
                **common,
                "kind": "single_core_baseline",
                "graph_path": str(data_dir / (case + ".json")),
            })
    for case in cases:
        for core in cores:
            row = development_scores.get((case, core))
            tasks.append({
                **common,
                "kind": "candidate_group",
                "graph_path": str(data_dir / (case + ".json")),
                "cores": core,
                "development_row": row,
            })
    if not args.allow_missing_development_manifests:
        failures = _development_preflight(
            tasks, candidate_root, development_scores, fingerprint)
        if failures:
            path = full_root / "development_manifest_preflight_failures.json"
            _write_json(path, failures)
            raise RuntimeError(
                "{} locked development manifests are missing or mismatched; "
                "copy them from the development server before the full run: {}".format(
                    len(failures), path))

    host = _host_resources()
    _write_json(full_root / "run_request.json", {
        "schema_version": 1,
        "cases": cases,
        "cores": list(cores),
        "include_single_baseline": not args.skip_single_baseline,
        "workers": args.workers,
        "fingerprint": fingerprint,
        "host": host,
        "requested_at": _utc_now(),
    })
    records: List[Dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        future_map = {executor.submit(_worker, task): task for task in tasks}
        completed = 0
        for future in as_completed(future_map):
            task = future_map[future]
            try:
                record = future.result()
            except Exception as error:
                record = {
                    "status": "failed",
                    "case": Path(task["graph_path"]).stem,
                    "cores": task.get("cores", 1),
                    "kind": task["kind"],
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            records.append(record)
            completed += 1
            score = record.get("winner") or {}
            print(
                "[{}/{}] {} k{} status={} winner={} makespan={} disposition={}".format(
                    completed, len(tasks), record.get("case"), record.get("cores"),
                    record.get("status"), score.get("name", "single"),
                    score.get("makespan", record.get("makespan")),
                    record.get("run_disposition")),
                flush=True,
            )
            _write_json(full_root / "latest_progress.json", {
                "completed_tasks": completed,
                "total_tasks": len(tasks),
                "last_record": record,
                "updated_at": _utc_now(),
            })

    return _summarize(
        cases=cases, cores=cores, full_root=full_root,
        fingerprint=fingerprint, host=host, workers=args.workers,
        invocation_wall=time.perf_counter() - invocation_started)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resumable 100-case Problem-1 experiment runner")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--config", default="data/config.txt")
    parser.add_argument("--output-root", default="artifacts/problem1_full")
    parser.add_argument(
        "--candidate-output-root", default="artifacts/problem1_candidates")
    parser.add_argument(
        "--cache-dir", default="artifacts/problem1_candidate_cache")
    parser.add_argument(
        "--development-summary", default="problem1_development_summary.json")
    parser.add_argument("--cases", nargs="+", help="subset such as case_076 case_091")
    parser.add_argument("--cores", nargs="+", type=int, default=list(DEFAULT_CORES))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--skip-single-baseline", action="store_true")
    parser.add_argument(
        "--allow-missing-development-manifests", action="store_true",
        help="allow re-execution when a locked 48-group development manifest is absent")
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    try:
        summary = run_full_experiment(args)
    except (CandidateManagerError, FileNotFoundError, RuntimeError, ValueError) as error:
        print("[FULL RUN ERROR] {}".format(error))
        return 2
    print("summary: {}".format(
        Path(args.output_root).resolve() / "problem1_full_summary.json"))
    print("failures: {}".format(summary["failure_count"]))
    print("resource recommendation: {}".format(
        summary["resource_recommendation"]["recommended_next_workers"]))
    return 0 if summary["failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
