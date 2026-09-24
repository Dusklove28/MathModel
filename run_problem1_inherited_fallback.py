"""Re-evaluate lower-core winners as deterministic fallback candidates.

This experiment is intentionally separate from the frozen six-candidate
manager.  For target core counts 3, 4, and 5 it loads every winner produced
at a lower core count (starting at k=2), preserves its node partition and
existing per-core schedules, and appends empty core schedules until the
target core count is reached.  Each unique padded plan is validated and
evaluated by the unchanged official Problem-1 evaluator.

The runner is resumable.  It never overwrites the original full-run records
or candidate plans, and its summaries label pre-evaluation projections and
officially measured values separately.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import os
import subprocess
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
    sha256_file,
)
from contest_io import _read_json
from evaluation_validation import validate_task_order
from stub_multicore_cut_and_schedule import derive_multicore_plan


TARGET_CORES = (3, 4, 5)
SOURCE_CORES = (2, 3, 4)
DEFAULT_ARTIFACT_ROOT = Path(
    os.environ.get("PROBLEM1_ARTIFACT_ROOT", "/media/data/yn"))
# These released runner revisions used the same inheritance/evaluation policy.
# They differ from the current runner only in portable storage/resume handling.
STORAGE_COMPATIBLE_PREVIOUS_RUNNER_HASHES = frozenset({
    "7b07d92bb3040a47de6f208adf482dafc76026aa2009dda591c575fcbe9463e7",
    "f6c91c18725c8660dd193a20c0e3131bfdaf6d31dfc634ce476ad55d212c4348",
})
FROZEN_KEYS = (
    "solver_sha256",
    "candidate_manager_sha256",
    "config_sha256",
    "evaluator_sha256",
    "evaluator_file_sha256",
    "candidate_priority",
)


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
    path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str],
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


def _git_commit(project_dir: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=project_dir,
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _score(record: Mapping[str, Any]) -> Tuple[int, int]:
    return int(record["makespan"]), int(record["added_copy_bytes"])


def _winner_score(group: Mapping[str, Any]) -> Tuple[int, int]:
    winner = group.get("winner") or {}
    if group.get("status") != "success":
        raise ValueError(
            "source group {} k{} is not successful".format(
                group.get("case"), group.get("cores")))
    if not isinstance(winner.get("makespan"), int):
        raise ValueError("source group winner has no integer makespan")
    if not isinstance(winner.get("added_copy_bytes"), int):
        raise ValueError("source group winner has no integer added_copy_bytes")
    return int(winner["makespan"]), int(winner["added_copy_bytes"])


def pad_plan_for_target(
    plan: Mapping[str, Any], target_cores: int,
) -> Dict[str, Any]:
    """Preserve a plan exactly and append empty schedules to target_cores."""

    if set(plan) != {"node_to_subgraph", "core_schedules"}:
        raise CandidateManagerError(
            "source plan must contain exactly node_to_subgraph and core_schedules")
    mapping = plan["node_to_subgraph"]
    schedules = plan["core_schedules"]
    if not isinstance(mapping, Mapping) or not isinstance(schedules, list):
        raise CandidateManagerError("source plan has invalid official fields")
    if (not isinstance(target_cores, int) or isinstance(target_cores, bool)
            or target_cores < len(schedules)):
        raise CandidateManagerError(
            "target core count cannot be smaller than source schedule count")
    if any(not isinstance(schedule, list) for schedule in schedules):
        raise CandidateManagerError("each source core schedule must be a list")
    return {
        "node_to_subgraph": copy.deepcopy(dict(mapping)),
        "core_schedules": copy.deepcopy(schedules)
        + [[] for _ in range(target_cores - len(schedules))],
    }


def _assert_inheritance(
    source_plan: Mapping[str, Any], inherited_plan: Mapping[str, Any],
    target_cores: int,
) -> None:
    source_schedules = source_plan["core_schedules"]
    inherited_schedules = inherited_plan["core_schedules"]
    if inherited_plan["node_to_subgraph"] != source_plan["node_to_subgraph"]:
        raise CandidateManagerError("inherited plan changed node_to_subgraph")
    if inherited_schedules[:len(source_schedules)] != source_schedules:
        raise CandidateManagerError("inherited plan changed an existing core schedule")
    if inherited_schedules[len(source_schedules):] != [
        [] for _ in range(target_cores - len(source_schedules))
    ]:
        raise CandidateManagerError("inherited plan did not append only empty cores")
    if len(inherited_schedules) != target_cores:
        raise CandidateManagerError("inherited plan has wrong target core count")


def _current_frozen_fingerprint(
    project_dir: Path, config_path: Path,
) -> Dict[str, Any]:
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


def _stable_fingerprint(fingerprint: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: fingerprint.get(key) for key in FROZEN_KEYS}


def _check_frozen_source(
    project_dir: Path, config_path: Path, full_root: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    path = full_root / "frozen_fingerprint.json"
    if not path.is_file():
        raise FileNotFoundError("full-run frozen fingerprint not found: {}".format(path))
    wrapper = _read_json(path)
    recorded = wrapper.get("fingerprint")
    if not isinstance(recorded, Mapping):
        raise ValueError("invalid full-run frozen fingerprint")
    current = _current_frozen_fingerprint(project_dir, config_path)
    if _stable_fingerprint(recorded) != _stable_fingerprint(current):
        raise RuntimeError(
            "frozen solver/candidate/config/evaluator fingerprint changed; "
            "run this experiment from the Problem-1 frozen version")
    return dict(recorded), current


def _experiment_identity(
    project_dir: Path, source_fingerprint: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "source_frozen_fingerprint": _stable_fingerprint(source_fingerprint),
        "fallback_runner_sha256": sha256_file(Path(__file__).resolve()),
        "fallback_policy": {
            "target_cores": list(TARGET_CORES),
            "minimum_source_cores": 2,
            "inherit_all_lower_original_winners": True,
            "preserve_node_to_subgraph": True,
            "preserve_existing_core_schedule_order": True,
            "padding": "append_empty_core_schedules",
            "selection_score": ["makespan", "added_copy_bytes"],
            "tie_priority": "original_target_before_inherited_lower_core",
        },
    }


def _identity_without_runner_hash(identity: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the scientific identity, excluding orchestration-only revisions."""

    return {
        key: copy.deepcopy(value)
        for key, value in identity.items()
        if key != "fallback_runner_sha256"
    }


def _compatible_runner_revision(
    recorded: Mapping[str, Any], current: Mapping[str, Any],
) -> bool:
    """Allow storage/resume fixes without changing the scheduling experiment."""

    return (
        recorded.get("fallback_runner_sha256")
        in STORAGE_COMPATIBLE_PREVIOUS_RUNNER_HASHES
        and
        _identity_without_runner_hash(recorded)
        == _identity_without_runner_hash(current)
    )


def _group_path(root: Path, case: str, cores: int) -> Path:
    return root / "groups" / case / "k{}.json".format(cores)


def _baseline_path(root: Path, case: str) -> Path:
    return root / "single_baselines" / case / "record.json"


def _fallback_group_path(root: Path, case: str, cores: int) -> Path:
    return root / "groups" / case / "k{}.json".format(cores)


def _candidate_record_path(
    root: Path, case: str, target_cores: int, source_cores: int,
) -> Path:
    return (
        root / "groups" / case / "k{}".format(target_cores)
        / "candidate_records" / "inherit_k{}.json".format(source_cores)
    )


def _candidate_plan_path(
    root: Path, case: str, target_cores: int, source_cores: int,
) -> Path:
    return (
        root / "groups" / case / "k{}".format(target_cores)
        / "candidates" / "inherit_k{}_multicore_res.json".format(source_cores)
    )


def _candidate_result_path(
    root: Path, case: str, target_cores: int, source_cores: int,
) -> Path:
    return (
        root / "groups" / case / "k{}".format(target_cores)
        / "official_results" / "inherit_k{}_evaluation.json".format(source_cores)
    )


def _load_full_group(full_root: Path, case: str, cores: int) -> Dict[str, Any]:
    path = _group_path(full_root, case, cores)
    if not path.is_file():
        raise FileNotFoundError("full-run group record not found: {}".format(path))
    group = _read_json(path)
    if group.get("case") != case or int(group.get("cores", -1)) != cores:
        raise ValueError("full-run group identity mismatch: {}".format(path))
    _winner_score(group)
    return dict(group)


def _resolve_original_final_plan(
    candidate_root: Path, group: Mapping[str, Any], case: str, cores: int,
) -> Path:
    deterministic = (
        candidate_root / case / "k{}".format(cores) / "final_multicore_res.json")
    if deterministic.is_file():
        return deterministic.resolve()
    stored = group.get("final_plan_path")
    if stored and Path(stored).is_file():
        return Path(stored).resolve()
    raise FileNotFoundError(
        "original final plan is unavailable for {} k{}; expected {}".format(
            case, cores, deterministic))


def _resolve_old_official_result(
    cache_dir: Path, group: Mapping[str, Any],
) -> Path | None:
    winner = group.get("winner") or {}
    signature = winner.get("canonical_signature")
    for candidate in group.get("candidates", []):
        if candidate.get("canonical_signature") != signature:
            continue
        stored = candidate.get("full_result_path")
        if stored:
            path = Path(stored)
            candidates = [path] if path.is_absolute() else [
                cache_dir / path,
                cache_dir / "results" / path.name,
            ]
            for candidate_path in candidates:
                if candidate_path.is_file():
                    return candidate_path.resolve()
        key = candidate.get("evaluation_key")
        if key:
            path = cache_dir / "results" / (str(key) + ".json")
            if path.is_file():
                return path.resolve()
    return None


def _selected_cases(data_dir: Path, requested: Sequence[str] | None) -> List[str]:
    if requested:
        cases = sorted(set(
            Path(name).stem if str(name).endswith(".json") else str(name)
            for name in requested
        ))
    else:
        cases = sorted(path.stem for path in data_dir.glob("case_*.json"))
    if not cases:
        raise ValueError("no input cases selected")
    missing = [case for case in cases if not (data_dir / (case + ".json")).is_file()]
    if missing:
        raise FileNotFoundError("input cases not found: {}".format(", ".join(missing)))
    return cases


def _projection_for_group(
    full_root: Path, case: str, target_cores: int,
) -> Dict[str, Any]:
    target = _load_full_group(full_root, case, target_cores)
    old_score = _winner_score(target)
    sources = []
    for source_cores in range(2, target_cores):
        source = _load_full_group(full_root, case, source_cores)
        source_score = _winner_score(source)
        sources.append({
            "source_cores": source_cores,
            "source_winner": (source.get("winner") or {}).get("name"),
            "projected_makespan": source_score[0],
            "projected_added_copy_bytes": source_score[1],
            "projection_basis": (
                "officially measured lower-core winner; padded plan not yet re-evaluated"),
        })
    best_source = min(
        sources,
        key=lambda row: (
            row["projected_makespan"], row["projected_added_copy_bytes"],
            row["source_cores"],
        ),
    )
    best_score = (
        int(best_source["projected_makespan"]),
        int(best_source["projected_added_copy_bytes"]),
    )
    selected = min(old_score, best_score)
    return {
        "case": case,
        "target_cores": target_cores,
        "old_winner": (target.get("winner") or {}).get("name"),
        "old_makespan": old_score[0],
        "old_added_copy_bytes": old_score[1],
        "source_candidates": sources,
        "best_source_cores": best_source["source_cores"],
        "projected_fallback_makespan": best_score[0],
        "projected_fallback_added_copy_bytes": best_score[1],
        "projected_selected_makespan": selected[0],
        "projected_selected_added_copy_bytes": selected[1],
        "makespan_degraded": old_score[0] > best_score[0],
        "movement_only_degraded": (
            old_score[0] == best_score[0] and old_score[1] > best_score[1]),
        "projected_makespan_recovery": max(0, old_score[0] - best_score[0]),
    }


def _preflight(
    *,
    full_root: Path,
    candidate_root: Path,
    cache_dir: Path,
    cases: Sequence[str],
    targets: Sequence[int],
) -> List[Dict[str, Any]]:
    """Resolve every immutable input before starting expensive evaluation."""

    failures: List[Dict[str, Any]] = []
    for case in cases:
        baseline = _baseline_path(full_root, case)
        if not baseline.is_file():
            failures.append({
                "case": case, "target_cores": 1,
                "reason": "missing_single_core_baseline", "path": str(baseline),
            })
        for target_cores in targets:
            for cores in range(2, target_cores + 1):
                try:
                    group = _load_full_group(full_root, case, cores)
                    plan_path = _resolve_original_final_plan(
                        candidate_root, group, case, cores)
                    plan = _read_json(plan_path)
                    if len(plan.get("core_schedules", [])) != cores:
                        raise CandidateManagerError(
                            "plan contains {} core schedules".format(
                                len(plan.get("core_schedules", []))))
                    signature = canonical_plan_signature(plan)
                    expected = (group.get("winner") or {}).get(
                        "canonical_signature")
                    if expected and signature != expected:
                        raise CandidateManagerError(
                            "final plan signature does not match winner record")
                    if cores == target_cores:
                        old_result = _resolve_old_official_result(
                            cache_dir, group)
                        if old_result is None:
                            raise FileNotFoundError(
                                "old winner official result is unavailable")
                except Exception as error:
                    failures.append({
                        "case": case,
                        "target_cores": target_cores,
                        "source_or_target_cores": cores,
                        "reason": type(error).__name__,
                        "error": str(error),
                    })
    unique: Dict[str, Dict[str, Any]] = {}
    for failure in failures:
        key = json.dumps(failure, ensure_ascii=False, sort_keys=True)
        unique[key] = failure
    return list(unique.values())


def _task_priority(projection: Mapping[str, Any]) -> Tuple[Any, ...]:
    return (
        0 if projection["makespan_degraded"] else
        1 if projection["movement_only_degraded"] else 2,
        -int(projection["projected_makespan_recovery"]),
        str(projection["case"]),
        int(projection["target_cores"]),
    )


def _load_cached_result(
    cache: EvaluationCache, entry: Mapping[str, Any], key: str,
) -> Dict[str, Any]:
    stored = entry.get("full_result_path")
    candidates: List[Path] = []
    if stored:
        path = Path(str(stored))
        candidates.append(path if path.is_absolute() else cache.root / path)
    candidates.append(cache.results_dir / (key + ".json"))
    for path in candidates:
        if path.is_file():
            result = _read_json(path)
            _validate_official_result(result)
            return dict(result)
    raise FileNotFoundError("cached official result is missing for key {}".format(key))


def _resumable_candidate(
    record_path: Path,
    *,
    experiment_identity: Mapping[str, Any],
    graph_hash: str,
    source_plan_hash: str,
    inherited_plan_hash: str,
    plan_path: Path,
    result_path: Path,
) -> Dict[str, Any] | None:
    if not record_path.is_file() or not result_path.is_file():
        return None
    try:
        record = _read_json(record_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    expected = {
        "status": "success",
        "experiment_identity": experiment_identity,
        "graph_sha256": graph_hash,
        "source_plan_sha256": source_plan_hash,
        "inherited_plan_sha256": inherited_plan_hash,
    }
    if any(record.get(key) != value for key, value in expected.items()):
        return None
    result_hash = record.get("official_result_sha256")
    if not result_hash or sha256_file(result_path) != result_hash:
        return None
    resumed = dict(record)
    resumed["run_disposition"] = "resumed_candidate_record"
    resumed["inherited_plan_path"] = str(plan_path.resolve())
    resumed["official_result_path"] = str(result_path.resolve())
    resumed["inherited_plan_file_sha256"] = sha256_file(plan_path)
    return resumed


def _evaluate_inherited_candidate(
    *,
    graph: Mapping[str, Any],
    graph_hash: str,
    config_path: Path,
    cache_dir: Path,
    output_root: Path,
    case: str,
    target_cores: int,
    source_cores: int,
    source_group: Mapping[str, Any],
    source_plan: Mapping[str, Any],
    experiment_identity: Mapping[str, Any],
) -> Dict[str, Any]:
    started = time.perf_counter()
    record_path = _candidate_record_path(
        output_root, case, target_cores, source_cores)
    plan_path = _candidate_plan_path(
        output_root, case, target_cores, source_cores)
    result_path = _candidate_result_path(
        output_root, case, target_cores, source_cores)
    source_plan_hash = canonical_plan_signature(source_plan)
    expected_source_hash = (source_group.get("winner") or {}).get(
        "canonical_signature")
    if expected_source_hash and source_plan_hash != expected_source_hash:
        raise CandidateManagerError(
            "{} k{} final plan hash does not match its winner record".format(
                case, source_cores))

    inherited = pad_plan_for_target(source_plan, target_cores)
    _assert_inheritance(source_plan, inherited, target_cores)
    inherited_hash = canonical_plan_signature(inherited)
    _write_json(plan_path, inherited)
    resumed = _resumable_candidate(
        record_path,
        experiment_identity=experiment_identity,
        graph_hash=graph_hash,
        source_plan_hash=source_plan_hash,
        inherited_plan_hash=inherited_hash,
        plan_path=plan_path,
        result_path=result_path,
    )
    if resumed is not None:
        return resumed

    projection = _winner_score(source_group)
    record: Dict[str, Any] = {
        "schema_version": 1,
        "kind": "inherited_fallback_candidate",
        "status": "pending",
        "case": case,
        "target_cores": target_cores,
        "source_cores": source_cores,
        "name": "inherit_k{}".format(source_cores),
        "source_winner": (source_group.get("winner") or {}).get("name"),
        "projected_makespan_before_reevaluation": projection[0],
        "projected_added_copy_bytes_before_reevaluation": projection[1],
        "projection_value_kind": "pre_reevaluation_inference",
        "projection_basis": (
            "lower-core official measurement; exact plan padded with empty cores"),
        "experiment_identity": dict(experiment_identity),
        "graph_sha256": graph_hash,
        "source_plan_sha256": source_plan_hash,
        "source_plan_canonical_sha256": source_plan_hash,
        "inherited_plan_sha256": inherited_hash,
        "inherited_plan_canonical_sha256": inherited_hash,
        "inherited_plan_file_sha256": sha256_file(plan_path),
        "source_plan_path": None,
        "inherited_plan_path": str(plan_path.resolve()),
        "official_result_path": str(result_path.resolve()),
        "validation": {
            "node_to_subgraph_unchanged": True,
            "existing_core_schedules_unchanged": True,
            "appended_empty_core_count": target_cores - source_cores,
            "official_plan_validation_passed": False,
        },
        "timestamp": _utc_now(),
    }
    try:
        validation_started = time.perf_counter()
        view = derive_multicore_plan(graph, inherited)
        validate_task_order(view)
        record["validation"]["official_plan_validation_passed"] = True
        record["validation_time_seconds"] = time.perf_counter() - validation_started

        config_hash = sha256_file(config_path)
        evaluator_hash, evaluator_files = official_evaluator_hash()
        key = evaluation_key(
            graph_hash, config_hash, inherited_hash, evaluator_hash)
        cache = EvaluationCache(cache_dir)
        lookup_started = time.perf_counter()
        entry = cache.lookup(
            graph_hash, config_hash, inherited_hash, evaluator_hash)
        lookup_time = time.perf_counter() - lookup_started
        evaluation_time = 0.0
        if entry is None:
            evaluation_started = time.perf_counter()
            official_result = evaluate_official_problem1(
                graph, inherited, config_path)
            evaluation_time = time.perf_counter() - evaluation_started
            makespan, added, max_memory = _validate_official_result(official_result)
            entry, _ = cache.store(
                graph_hash=graph_hash,
                config_hash=config_hash,
                plan_hash=inherited_hash,
                evaluator_hash=evaluator_hash,
                result=official_result,
                evaluation_time=evaluation_time,
                max_memory_bytes=max_memory,
                metadata={
                    "graph": case + ".json",
                    "candidate": "inherit_k{}".format(source_cores),
                    "source_cores": source_cores,
                    "target_cores": target_cores,
                    "experiment": "problem1_inherited_fallback",
                },
            )
            disposition = "official_evaluation"
        else:
            official_result = _load_cached_result(cache, entry, key)
            makespan, added, max_memory = _validate_official_result(official_result)
            disposition = "official_cache_hit"
        _write_json(result_path, official_result)
        record.update({
            "status": "success",
            "run_disposition": disposition,
            "evaluation_key": key,
            "official_measurement_value_kind": "officially_measured",
            "measured_makespan": makespan,
            "measured_added_copy_bytes": added,
            "max_memory_bytes": max_memory,
            "projection_makespan_error": makespan - projection[0],
            "projection_added_copy_bytes_error": added - projection[1],
            "projection_exact_match": (
                makespan == projection[0] and added == projection[1]),
            "config_sha256": config_hash,
            "evaluator_sha256": evaluator_hash,
            "evaluator_file_sha256": evaluator_files,
            "evaluation_key": key,
            "cache_lookup_time_seconds": lookup_time,
            "official_evaluation_time_seconds": evaluation_time,
            "official_result_sha256": sha256_file(result_path),
            "wall_time_seconds": time.perf_counter() - started,
            "timestamp": _utc_now(),
        })
    except Exception as error:
        record.update({
            "status": "failed",
            "run_disposition": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
            "wall_time_seconds": time.perf_counter() - started,
            "timestamp": _utc_now(),
        })
    _write_json(record_path, record)
    return record


def _candidate_score(candidate: Mapping[str, Any]) -> Tuple[int, int]:
    return (
        int(candidate["measured_makespan"]),
        int(candidate["measured_added_copy_bytes"]),
    )


def select_final_option(
    old_score: Tuple[int, int], candidates: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Select lexicographically, retaining the original plan on an exact tie."""

    options: List[Dict[str, Any]] = [{
        "name": "original_target_winner",
        "source_cores": None,
        "makespan": old_score[0],
        "added_copy_bytes": old_score[1],
        "priority": 0,
    }]
    for candidate in candidates:
        if candidate.get("status") != "success":
            continue
        options.append({
            "name": candidate["name"],
            "source_cores": int(candidate["source_cores"]),
            "makespan": int(candidate["measured_makespan"]),
            "added_copy_bytes": int(candidate["measured_added_copy_bytes"]),
            "priority": 1 + int(candidate["source_cores"]),
        })
    return min(
        options,
        key=lambda option: (
            option["makespan"], option["added_copy_bytes"], option["priority"]),
    )


def _resumable_group(
    path: Path, experiment_identity: Mapping[str, Any],
) -> Dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        record = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (record.get("status") != "success"
            or record.get("experiment_identity") != experiment_identity):
        return None
    relocated_run_dir = path.with_suffix("")
    final_plan = relocated_run_dir / "final_multicore_res.json"
    final_result = relocated_run_dir / "final_official_evaluation.json"
    if not final_plan.is_file() or not final_result.is_file():
        return None
    resumed = dict(record)
    resumed["run_disposition"] = "resumed_group_record"
    resumed["final_plan_path"] = str(final_plan.resolve())
    resumed["final_official_result_path"] = str(final_result.resolve())
    resumed["final_plan_file_sha256"] = sha256_file(final_plan)
    resumed["final_official_result_sha256"] = sha256_file(final_result)
    return resumed


def _relocate_resumed_group_paths(
    record: Dict[str, Any],
    *,
    group_record_path: Path,
    candidate_root: Path,
    output_root: Path,
    case: str,
    target_cores: int,
) -> Dict[str, Any]:
    """Rewrite stale absolute paths after moving an experiment directory."""

    record["old_plan_path"] = str((
        candidate_root / case / "k{}".format(target_cores)
        / "final_multicore_res.json").resolve())
    relocated_candidates: List[Dict[str, Any]] = []
    for raw_candidate in record.get("fallback_candidates", []):
        candidate = dict(raw_candidate)
        source_cores = int(candidate["source_cores"])
        candidate["source_plan_path"] = str((
            candidate_root / case / "k{}".format(source_cores)
            / "final_multicore_res.json").resolve())
        plan_path = _candidate_plan_path(
            output_root, case, target_cores, source_cores)
        candidate["inherited_plan_path"] = str(plan_path.resolve())
        if plan_path.is_file():
            candidate["inherited_plan_file_sha256"] = sha256_file(plan_path)
        result_source_cores = source_cores
        if candidate.get("run_disposition") == "deduplicated_inherited_plan":
            canonical = str(candidate.get("canonical_candidate", ""))
            if canonical.startswith("inherit_k"):
                result_source_cores = int(canonical[len("inherit_k"):])
        result_path = _candidate_result_path(
            output_root, case, target_cores, result_source_cores)
        candidate["official_result_path"] = str(result_path.resolve())
        relocated_candidates.append(candidate)
        candidate_record_path = _candidate_record_path(
            output_root, case, target_cores, source_cores)
        if candidate_record_path.is_file():
            _write_json(candidate_record_path, candidate)
    record["fallback_candidates"] = relocated_candidates
    _write_json(group_record_path, record)
    return record


def _run_group(task: Mapping[str, Any]) -> Dict[str, Any]:
    started = time.perf_counter()
    full_root = Path(task["full_root"])
    candidate_root = Path(task["candidate_root"])
    cache_dir = Path(task["cache_dir"])
    output_root = Path(task["output_root"])
    data_dir = Path(task["data_dir"])
    config_path = Path(task["config_path"])
    case = str(task["case"])
    target_cores = int(task["target_cores"])
    identity = task["experiment_identity"]
    group_record_path = _fallback_group_path(
        output_root, case, target_cores)
    resumed = _resumable_group(group_record_path, identity)
    if resumed is not None:
        return _relocate_resumed_group_paths(
            resumed,
            group_record_path=group_record_path,
            candidate_root=candidate_root,
            output_root=output_root,
            case=case,
            target_cores=target_cores,
        )

    graph_path = data_dir / (case + ".json")
    graph_hash = sha256_file(graph_path)
    graph = _read_json(graph_path)
    old_group = _load_full_group(full_root, case, target_cores)
    old_score = _winner_score(old_group)
    old_plan_path = _resolve_original_final_plan(
        candidate_root, old_group, case, target_cores)
    old_plan = _read_json(old_plan_path)
    if len(old_plan.get("core_schedules", [])) != target_cores:
        raise CandidateManagerError(
            "{} k{} old final plan has {} core schedules".format(
                case, target_cores, len(old_plan.get("core_schedules", []))))
    old_plan_hash = canonical_plan_signature(old_plan)
    expected_old_hash = (old_group.get("winner") or {}).get(
        "canonical_signature")
    if expected_old_hash and old_plan_hash != expected_old_hash:
        raise CandidateManagerError(
            "{} k{} old final plan hash does not match its record".format(
                case, target_cores))

    candidate_records: List[Dict[str, Any]] = []
    signatures: Dict[str, Dict[str, Any]] = {}
    for source_cores in range(2, target_cores):
        source_group = _load_full_group(full_root, case, source_cores)
        source_plan_path = _resolve_original_final_plan(
            candidate_root, source_group, case, source_cores)
        source_plan = _read_json(source_plan_path)
        if len(source_plan.get("core_schedules", [])) != source_cores:
            raise CandidateManagerError(
                "{} k{} source final plan has {} core schedules".format(
                    case, source_cores,
                    len(source_plan.get("core_schedules", []))))
        inherited = pad_plan_for_target(source_plan, target_cores)
        signature = canonical_plan_signature(inherited)
        if signature in signatures:
            canonical = signatures[signature]
            alias = dict(canonical)
            projected_score = _winner_score(source_group)
            measured_score = _candidate_score(canonical)
            source_plan_hash = canonical_plan_signature(source_plan)
            alias_plan_path = _candidate_plan_path(
                output_root, case, target_cores, source_cores)
            _write_json(alias_plan_path, inherited)
            alias.update({
                "name": "inherit_k{}".format(source_cores),
                "source_cores": source_cores,
                "source_winner": (source_group.get("winner") or {}).get("name"),
                "projected_makespan_before_reevaluation": projected_score[0],
                "projected_added_copy_bytes_before_reevaluation": projected_score[1],
                "projection_makespan_error": measured_score[0] - projected_score[0],
                "projection_added_copy_bytes_error": (
                    measured_score[1] - projected_score[1]),
                "projection_exact_match": measured_score == projected_score,
                "run_disposition": "deduplicated_inherited_plan",
                "canonical_candidate": canonical["name"],
                "equivalent_inherited_plan": True,
                "source_plan_path": str(source_plan_path),
                "source_plan_file_sha256": sha256_file(source_plan_path),
                "source_plan_sha256": source_plan_hash,
                "source_plan_canonical_sha256": source_plan_hash,
                "inherited_plan_path": str(alias_plan_path.resolve()),
                "inherited_plan_file_sha256": sha256_file(alias_plan_path),
                "validation": {
                    **dict(canonical.get("validation", {})),
                    "appended_empty_core_count": target_cores - source_cores,
                },
            })
            candidate_records.append(alias)
            _write_json(
                _candidate_record_path(
                    output_root, case, target_cores, source_cores), alias)
            continue
        record = _evaluate_inherited_candidate(
            graph=graph,
            graph_hash=graph_hash,
            config_path=config_path,
            cache_dir=cache_dir,
            output_root=output_root,
            case=case,
            target_cores=target_cores,
            source_cores=source_cores,
            source_group=source_group,
            source_plan=source_plan,
            experiment_identity=identity,
        )
        record["source_plan_path"] = str(source_plan_path)
        record["source_plan_file_sha256"] = sha256_file(source_plan_path)
        _write_json(
            _candidate_record_path(
                output_root, case, target_cores, source_cores), record)
        candidate_records.append(record)
        if record.get("status") == "success":
            signatures[signature] = record

    failed = [
        record for record in candidate_records
        if record.get("status") != "success"
    ]
    final_option = select_final_option(old_score, candidate_records)
    run_dir = output_root / "groups" / case / "k{}".format(target_cores)
    final_plan_path = run_dir / "final_multicore_res.json"
    final_result_path = run_dir / "final_official_evaluation.json"
    if final_option["name"] == "original_target_winner":
        final_plan = old_plan
        old_result = _resolve_old_official_result(cache_dir, old_group)
        if old_result is None:
            raise FileNotFoundError(
                "old winner official result missing for {} k{}".format(
                    case, target_cores))
        final_result = _read_json(old_result)
        final_source = {
            "kind": "original_target_winner",
            "old_winner": (old_group.get("winner") or {}).get("name"),
            "source_cores": target_cores,
        }
    else:
        source_cores = int(final_option["source_cores"])
        final_plan = _read_json(_candidate_plan_path(
            output_root, case, target_cores, source_cores))
        final_result = _read_json(_candidate_result_path(
            output_root, case, target_cores, source_cores))
        final_source = {
            "kind": "inherited_fallback",
            "name": final_option["name"],
            "source_cores": source_cores,
        }
    measured_makespan, measured_added, _ = _validate_official_result(final_result)
    if (measured_makespan, measured_added) != (
        final_option["makespan"], final_option["added_copy_bytes"]
    ):
        raise CandidateManagerError("selected final result does not match its score")
    final_view = derive_multicore_plan(graph, final_plan)
    validate_task_order(final_view)
    _write_json(final_plan_path, final_plan)
    _write_json(final_result_path, final_result)

    projection = _projection_for_group(full_root, case, target_cores)
    status = "success" if not failed else "failed"
    record = {
        "schema_version": 1,
        "kind": "inherited_fallback_group",
        "status": status,
        "run_disposition": "executed",
        "case": case,
        "target_cores": target_cores,
        "experiment_identity": dict(identity),
        "graph_sha256": graph_hash,
        "old_winner": (old_group.get("winner") or {}).get("name"),
        "old_makespan": old_score[0],
        "old_added_copy_bytes": old_score[1],
        "old_plan_path": str(old_plan_path),
        "old_plan_sha256": old_plan_hash,
        "old_plan_canonical_sha256": old_plan_hash,
        "old_plan_file_sha256": sha256_file(old_plan_path),
        "pre_reevaluation_projection": projection,
        "fallback_candidates": candidate_records,
        "failed_candidates": [record.get("name") for record in failed],
        "final_source": final_source,
        "final_makespan": measured_makespan,
        "final_added_copy_bytes": measured_added,
        "final_value_kind": "officially_measured",
        "final_plan_path": str(final_plan_path.resolve()),
        "final_plan_sha256": canonical_plan_signature(final_plan),
        "final_plan_canonical_sha256": canonical_plan_signature(final_plan),
        "final_plan_file_sha256": sha256_file(final_plan_path),
        "final_official_result_path": str(final_result_path.resolve()),
        "final_official_result_sha256": sha256_file(final_result_path),
        "makespan_improvement": old_score[0] - measured_makespan,
        "added_copy_bytes_change": measured_added - old_score[1],
        "projection_exact_for_all_candidates": all(
            candidate.get("projection_exact_match", False)
            for candidate in candidate_records
        ),
        "wall_time_seconds": time.perf_counter() - started,
        "timestamp": _utc_now(),
    }
    _write_json(group_record_path, record)
    return record


def _worker(task: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        return _run_group(task)
    except Exception as error:
        case = str(task["case"])
        target_cores = int(task["target_cores"])
        record = {
            "schema_version": 1,
            "kind": "inherited_fallback_group",
            "status": "failed",
            "run_disposition": "failed",
            "case": case,
            "target_cores": target_cores,
            "experiment_identity": dict(task["experiment_identity"]),
            "error_type": type(error).__name__,
            "error": str(error),
            "timestamp": _utc_now(),
        }
        _write_json(_fallback_group_path(
            Path(task["output_root"]), case, target_cores), record)
        return record


def _geometric_mean(values: Sequence[float]) -> float | None:
    if not values or any(value <= 0 for value in values):
        return None
    return math.exp(sum(math.log(value) for value in values) / len(values))


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _result_row(
    *,
    full_root: Path,
    output_root: Path,
    case: str,
    cores: int,
    baseline: Mapping[str, Any],
) -> Dict[str, Any]:
    t1 = int(baseline["makespan"])
    if cores == 1:
        old_makespan = t1
        old_added = int(baseline["added_copy_bytes"])
        return {
            "case": case,
            "cores": 1,
            "t_i_1": t1,
            "old_winner": "single_core_baseline",
            "old_makespan": old_makespan,
            "old_added_copy_bytes": old_added,
            "old_speedup": 1.0,
            "projected_fallback_source_cores": "",
            "projected_fallback_makespan": "",
            "projected_fallback_added_copy_bytes": "",
            "pre_reevaluation_projected_winner": "single_core_baseline",
            "pre_reevaluation_projected_makespan": old_makespan,
            "pre_reevaluation_projected_added_copy_bytes": old_added,
            "pre_reevaluation_projected_speedup": 1.0,
            "measured_fallback_best_source_cores": "",
            "measured_fallback_makespan": "",
            "measured_fallback_added_copy_bytes": "",
            "final_winner": "single_core_baseline",
            "final_makespan": old_makespan,
            "final_added_copy_bytes": old_added,
            "final_speedup": 1.0,
            "reported_value_kind": "officially_measured_original",
            "officially_measured": True,
            "makespan_improvement": 0,
            "added_copy_bytes_change": 0,
            "projection_makespan_error": 0,
            "projection_added_copy_bytes_error": 0,
            "fallback_group_status": "not_applicable",
        }

    old_group = _load_full_group(full_root, case, cores)
    old_score = _winner_score(old_group)
    old_winner = (old_group.get("winner") or {}).get("name")
    if cores == 2:
        return {
            "case": case,
            "cores": 2,
            "t_i_1": t1,
            "old_winner": old_winner,
            "old_makespan": old_score[0],
            "old_added_copy_bytes": old_score[1],
            "old_speedup": t1 / old_score[0],
            "projected_fallback_source_cores": "",
            "projected_fallback_makespan": "",
            "projected_fallback_added_copy_bytes": "",
            "pre_reevaluation_projected_winner": old_winner,
            "pre_reevaluation_projected_makespan": old_score[0],
            "pre_reevaluation_projected_added_copy_bytes": old_score[1],
            "pre_reevaluation_projected_speedup": t1 / old_score[0],
            "measured_fallback_best_source_cores": "",
            "measured_fallback_makespan": "",
            "measured_fallback_added_copy_bytes": "",
            "final_winner": old_winner,
            "final_makespan": old_score[0],
            "final_added_copy_bytes": old_score[1],
            "final_speedup": t1 / old_score[0],
            "reported_value_kind": "officially_measured_original",
            "officially_measured": True,
            "makespan_improvement": 0,
            "added_copy_bytes_change": 0,
            "projection_makespan_error": 0,
            "projection_added_copy_bytes_error": 0,
            "fallback_group_status": "not_applicable",
        }

    projection = _projection_for_group(full_root, case, cores)
    projected_fallback = (
        projection["projected_fallback_makespan"],
        projection["projected_fallback_added_copy_bytes"],
    )
    projected_old = (old_score[0], old_score[1])
    if projected_fallback < projected_old:
        projected_winner = "inherit_k{}".format(projection["best_source_cores"])
    else:
        projected_winner = old_winner
    path = _fallback_group_path(output_root, case, cores)
    fallback_group = _read_json(path) if path.is_file() else None
    group_success = (
        isinstance(fallback_group, Mapping)
        and fallback_group.get("status") == "success"
    )
    if group_success:
        successful_candidates = [
            candidate for candidate in fallback_group.get("fallback_candidates", [])
            if candidate.get("status") == "success"
        ]
        best_candidate = min(
            successful_candidates,
            key=lambda candidate: (
                int(candidate["measured_makespan"]),
                int(candidate["measured_added_copy_bytes"]),
                int(candidate["source_cores"]),
            ),
        )
        final_makespan = int(fallback_group["final_makespan"])
        final_added = int(fallback_group["final_added_copy_bytes"])
        final_source = fallback_group.get("final_source") or {}
        final_winner = (
            old_winner if final_source.get("kind") == "original_target_winner"
            else final_source.get("name")
        )
        kind = "officially_measured_after_fallback"
        officially_measured = True
        measured_source = int(best_candidate["source_cores"])
        measured_fallback_makespan = int(best_candidate["measured_makespan"])
        measured_fallback_added = int(best_candidate["measured_added_copy_bytes"])
        projection_makespan_error = (
            measured_fallback_makespan
            - int(projection["projected_fallback_makespan"])
        )
        projection_added_error = (
            measured_fallback_added
            - int(projection["projected_fallback_added_copy_bytes"])
        )
        group_status = "success"
    else:
        final_makespan = int(projection["projected_selected_makespan"])
        final_added = int(projection["projected_selected_added_copy_bytes"])
        final_winner = projected_winner
        kind = "pre_reevaluation_projection_not_official_measurement"
        officially_measured = False
        measured_source = ""
        measured_fallback_makespan = ""
        measured_fallback_added = ""
        projection_makespan_error = ""
        projection_added_error = ""
        group_status = (
            fallback_group.get("status")
            if isinstance(fallback_group, Mapping) else "not_evaluated")
    return {
        "case": case,
        "cores": cores,
        "t_i_1": t1,
        "old_winner": old_winner,
        "old_makespan": old_score[0],
        "old_added_copy_bytes": old_score[1],
        "old_speedup": t1 / old_score[0],
        "projected_fallback_source_cores": projection["best_source_cores"],
        "projected_fallback_makespan": projection["projected_fallback_makespan"],
        "projected_fallback_added_copy_bytes": (
            projection["projected_fallback_added_copy_bytes"]),
        "pre_reevaluation_projected_winner": projected_winner,
        "pre_reevaluation_projected_makespan": (
            projection["projected_selected_makespan"]),
        "pre_reevaluation_projected_added_copy_bytes": (
            projection["projected_selected_added_copy_bytes"]),
        "pre_reevaluation_projected_speedup": (
            t1 / int(projection["projected_selected_makespan"])),
        "measured_fallback_best_source_cores": measured_source,
        "measured_fallback_makespan": measured_fallback_makespan,
        "measured_fallback_added_copy_bytes": measured_fallback_added,
        "final_winner": final_winner,
        "final_makespan": final_makespan,
        "final_added_copy_bytes": final_added,
        "final_speedup": t1 / final_makespan,
        "reported_value_kind": kind,
        "officially_measured": officially_measured,
        "makespan_improvement": old_score[0] - final_makespan,
        "added_copy_bytes_change": final_added - old_score[1],
        "projection_makespan_error": projection_makespan_error,
        "projection_added_copy_bytes_error": projection_added_error,
        "fallback_group_status": group_status,
    }


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    aggregates: List[Dict[str, Any]] = []
    for cores in range(1, 6):
        core_rows = [row for row in rows if int(row["cores"]) == cores]
        old_speedups = [float(row["old_speedup"]) for row in core_rows]
        projected_speedups = [
            float(row["pre_reevaluation_projected_speedup"])
            for row in core_rows
        ]
        measured_rows = [row for row in core_rows if row["officially_measured"]]
        measured_speedups = [float(row["final_speedup"]) for row in measured_rows]
        final_speedups = [float(row["final_speedup"]) for row in core_rows]
        total_t1 = sum(int(row["t_i_1"]) for row in core_rows)
        measured_t1 = sum(int(row["t_i_1"]) for row in measured_rows)
        measured_tk = sum(int(row["final_makespan"]) for row in measured_rows)
        aggregates.append({
            "cores": cores,
            "case_count": len(core_rows),
            "officially_measured_case_count": len(measured_rows),
            "projected_only_case_count": len(core_rows) - len(measured_rows),
            "old_arithmetic_mean_speedup": (
                sum(old_speedups) / len(old_speedups) if old_speedups else None),
            "pre_reevaluation_projected_arithmetic_mean_speedup": (
                sum(projected_speedups) / len(projected_speedups)
                if projected_speedups else None),
            "officially_measured_arithmetic_mean_speedup": (
                sum(measured_speedups) / len(measured_speedups)
                if measured_speedups else None),
            "reported_arithmetic_mean_speedup": (
                sum(final_speedups) / len(final_speedups)
                if final_speedups else None),
            "reported_median_speedup": _median(final_speedups),
            "reported_geometric_mean_speedup": _geometric_mean(final_speedups),
            "officially_measured_weighted_speedup": (
                measured_t1 / measured_tk if measured_tk else None),
            "reported_total_added_copy_bytes": sum(
                int(row["final_added_copy_bytes"]) for row in core_rows),
            "reported_mean_added_copy_bytes": (
                sum(int(row["final_added_copy_bytes"]) for row in core_rows)
                / len(core_rows) if core_rows else None),
            "total_makespan_improvement": sum(
                int(row["makespan_improvement"]) for row in core_rows),
            "improved_case_count": sum(
                int(row["makespan_improvement"]) > 0 for row in core_rows),
            "all_reported_values_officially_measured": (
                len(core_rows) == len(measured_rows)),
            "reported_value_kind": (
                "officially_measured"
                if len(core_rows) == len(measured_rows)
                else "mixed_official_measurements_and_pre_reevaluation_projections"
            ),
            "total_t_i_1": total_t1,
        })
    return aggregates


def _candidate_rows(
    output_root: Path, cases: Sequence[str], targets: Sequence[int],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for case in cases:
        for target in targets:
            path = _fallback_group_path(output_root, case, target)
            if not path.is_file():
                continue
            group = _read_json(path)
            for candidate in group.get("fallback_candidates", []):
                rows.append({
                    "case": case,
                    "target_cores": target,
                    "source_cores": candidate.get("source_cores"),
                    "candidate": candidate.get("name"),
                    "source_winner": candidate.get("source_winner"),
                    "status": candidate.get("status"),
                    "run_disposition": candidate.get("run_disposition"),
                    "projected_makespan_before_reevaluation": candidate.get(
                        "projected_makespan_before_reevaluation"),
                    "projected_added_copy_bytes_before_reevaluation": candidate.get(
                        "projected_added_copy_bytes_before_reevaluation"),
                    "measured_makespan": candidate.get("measured_makespan"),
                    "measured_added_copy_bytes": candidate.get(
                        "measured_added_copy_bytes"),
                    "projection_makespan_error": candidate.get(
                        "projection_makespan_error"),
                    "projection_added_copy_bytes_error": candidate.get(
                        "projection_added_copy_bytes_error"),
                    "projection_exact_match": candidate.get(
                        "projection_exact_match"),
                    "official_plan_validation_passed": (
                        candidate.get("validation", {}).get(
                            "official_plan_validation_passed")),
                    "inherited_plan_sha256": candidate.get(
                        "inherited_plan_sha256"),
                    "official_result_sha256": candidate.get(
                        "official_result_sha256"),
                    "inherited_plan_path": candidate.get("inherited_plan_path"),
                    "official_result_path": candidate.get("official_result_path"),
                    "error": candidate.get("error"),
                })
    return rows


def _summarize(
    *,
    full_root: Path,
    output_root: Path,
    cases: Sequence[str],
    targets: Sequence[int],
    source_fingerprint: Mapping[str, Any],
    current_fingerprint: Mapping[str, Any],
    experiment_identity: Mapping[str, Any],
    workers: int,
    invocation_wall: float,
    degraded_only: bool,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for case in cases:
        baseline_path = _baseline_path(full_root, case)
        if not baseline_path.is_file():
            failures.append({
                "case": case, "cores": 1, "scope": "baseline",
                "item": "single", "error": "missing baseline record",
            })
            continue
        baseline = _read_json(baseline_path)
        if baseline.get("status") != "success":
            failures.append({
                "case": case, "cores": 1, "scope": "baseline",
                "item": "single", "error": baseline.get("error", "failed"),
            })
            continue
        for cores in range(1, 6):
            try:
                rows.append(_result_row(
                    full_root=full_root,
                    output_root=output_root,
                    case=case,
                    cores=cores,
                    baseline=baseline,
                ))
            except Exception as error:
                failures.append({
                    "case": case, "cores": cores, "scope": "summary",
                    "item": "", "error": str(error),
                })
    for case in cases:
        for target in targets:
            path = _fallback_group_path(output_root, case, target)
            if not path.is_file():
                continue
            group = _read_json(path)
            if group.get("status") != "success":
                failures.append({
                    "case": case,
                    "cores": target,
                    "scope": "fallback_group",
                    "item": "",
                    "error": group.get("error") or ";".join(
                        group.get("failed_candidates", [])),
                })
    rows.sort(key=lambda row: (row["case"], int(row["cores"])))
    aggregate_rows = _aggregate(rows)
    candidate_rows = _candidate_rows(output_root, cases, targets)
    expected_groups = len(cases) * len(targets)
    successful_groups = sum(
        _fallback_group_path(output_root, case, target).is_file()
        and _read_json(_fallback_group_path(
            output_root, case, target)).get("status") == "success"
        for case in cases for target in targets
    )
    exact_candidates = [
        row for row in candidate_rows if row["status"] == "success"
        and row["projection_exact_match"] is True
    ]
    measured_candidates = [
        row for row in candidate_rows if row["status"] == "success"
    ]
    unique_measured_candidates = [
        row for row in measured_candidates
        if row["run_disposition"] != "deduplicated_inherited_plan"
    ]
    summary = {
        "schema_version": 1,
        "experiment": "problem1_inherited_lower_core_fallback",
        "source_full_root": str(full_root.resolve()),
        "output_root": str(output_root.resolve()),
        "source_frozen_fingerprint": dict(source_fingerprint),
        "current_fingerprint": dict(current_fingerprint),
        "experiment_identity": dict(experiment_identity),
        "workers": workers,
        "degraded_only_invocation": degraded_only,
        "selected_case_count": len(cases),
        "selected_cases": list(cases),
        "target_cores": list(targets),
        "expected_fallback_group_count": expected_groups,
        "successful_fallback_group_count": successful_groups,
        "complete": successful_groups == expected_groups and not failures,
        "invocation_wall_time_seconds": invocation_wall,
        "candidate_record_count": len(candidate_rows),
        "candidate_with_official_measurement_count": len(measured_candidates),
        "unique_inherited_plan_official_measurement_count": len(
            unique_measured_candidates),
        "deduplicated_candidate_count": (
            len(measured_candidates) - len(unique_measured_candidates)),
        "projection_exact_candidate_count": len(exact_candidates),
        "projection_mismatch_candidate_count": (
            len(measured_candidates) - len(exact_candidates)),
        "aggregate_by_cores": aggregate_rows,
        "failure_count": len(failures),
        "failures": failures,
        "results": rows,
        "fallback_candidates": candidate_rows,
        "timestamp": _utc_now(),
    }
    _write_json(
        output_root / "problem1_inherited_fallback_summary.json", summary)
    result_fields = (
        "case", "cores", "t_i_1", "old_winner", "old_makespan",
        "old_added_copy_bytes", "old_speedup",
        "pre_reevaluation_projected_winner",
        "pre_reevaluation_projected_makespan",
        "pre_reevaluation_projected_added_copy_bytes",
        "pre_reevaluation_projected_speedup", "final_winner",
        "final_makespan", "final_added_copy_bytes", "final_speedup",
        "reported_value_kind", "officially_measured", "makespan_improvement",
        "added_copy_bytes_change", "fallback_group_status",
    )
    _write_csv(
        output_root / "problem1_inherited_fallback_results.csv",
        rows, result_fields)
    _write_csv(
        output_root / "problem1_inherited_fallback_aggregate.csv",
        aggregate_rows, (
            "cores", "case_count", "officially_measured_case_count",
            "projected_only_case_count", "old_arithmetic_mean_speedup",
            "pre_reevaluation_projected_arithmetic_mean_speedup",
            "officially_measured_arithmetic_mean_speedup",
            "reported_arithmetic_mean_speedup", "reported_median_speedup",
            "reported_geometric_mean_speedup",
            "officially_measured_weighted_speedup",
            "reported_total_added_copy_bytes", "reported_mean_added_copy_bytes",
            "total_makespan_improvement", "improved_case_count",
            "all_reported_values_officially_measured", "reported_value_kind",
            "total_t_i_1",
        ))
    _write_csv(
        output_root / "problem1_inherited_fallback_appendix.csv",
        rows, (
            "case", "cores", "t_i_1", "old_winner", "old_makespan",
            "old_added_copy_bytes", "old_speedup",
            "projected_fallback_source_cores", "projected_fallback_makespan",
            "projected_fallback_added_copy_bytes",
            "pre_reevaluation_projected_winner",
            "pre_reevaluation_projected_makespan",
            "pre_reevaluation_projected_added_copy_bytes",
            "pre_reevaluation_projected_speedup",
            "measured_fallback_best_source_cores", "measured_fallback_makespan",
            "measured_fallback_added_copy_bytes", "final_winner",
            "final_makespan", "final_added_copy_bytes", "final_speedup",
            "reported_value_kind", "officially_measured", "makespan_improvement",
            "added_copy_bytes_change", "projection_makespan_error",
            "projection_added_copy_bytes_error", "fallback_group_status",
        ))
    _write_csv(
        output_root / "problem1_inherited_fallback_candidates.csv",
        candidate_rows, (
            "case", "target_cores", "source_cores", "candidate",
            "source_winner", "status", "run_disposition",
            "projected_makespan_before_reevaluation",
            "projected_added_copy_bytes_before_reevaluation",
            "measured_makespan", "measured_added_copy_bytes",
            "projection_makespan_error", "projection_added_copy_bytes_error",
            "projection_exact_match", "official_plan_validation_passed",
            "inherited_plan_sha256", "official_result_sha256",
            "inherited_plan_path", "official_result_path", "error",
        ))
    _write_csv(
        output_root / "problem1_inherited_fallback_failures.csv",
        failures, ("case", "cores", "scope", "item", "error"))
    return summary


def run_experiment(args: argparse.Namespace) -> Dict[str, Any]:
    invocation_started = time.perf_counter()
    project_dir = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir).resolve()
    config_path = Path(args.config).resolve()
    artifact_root = Path(args.artifact_root).expanduser().resolve()
    full_root = Path(
        args.full_root or artifact_root / "problem1_full_c4140").resolve()
    candidate_root = Path(
        args.candidate_output_root
        or artifact_root / "problem1_candidates").resolve()
    cache_dir = Path(
        args.cache_dir
        or artifact_root / "problem1_candidate_cache").resolve()
    output_root = Path(
        args.output_root
        or artifact_root / "problem1_inherited_fallback_c4140").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    cases = _selected_cases(data_dir, args.cases)
    targets = tuple(sorted(set(args.targets)))
    if any(target not in TARGET_CORES for target in targets):
        raise ValueError("targets must be selected from 3, 4, 5")
    source_fingerprint, current_fingerprint = _check_frozen_source(
        project_dir, config_path, full_root)
    computed_identity = _experiment_identity(project_dir, source_fingerprint)
    identity = computed_identity
    compatible_runner_update = False
    identity_path = output_root / "frozen_fallback_experiment.json"
    if identity_path.is_file():
        identity_wrapper = _read_json(identity_path)
        existing_identity = identity_wrapper.get("experiment_identity")
        if existing_identity != computed_identity:
            if (isinstance(existing_identity, Mapping)
                    and _compatible_runner_revision(
                        existing_identity, computed_identity)):
                identity = dict(existing_identity)
                compatible_runner_update = True
                revisions = list(identity_wrapper.get(
                    "compatible_runner_revisions", []))
                current_revision = {
                    "runner_sha256": computed_identity["fallback_runner_sha256"],
                    "git_commit": _git_commit(project_dir),
                    "artifact_root": str(artifact_root),
                    "reason": "portable artifact-root and resume-path update",
                    "recorded_at": _utc_now(),
                }
                if current_revision["runner_sha256"] not in {
                    item.get("runner_sha256") for item in revisions
                    if isinstance(item, Mapping)
                }:
                    revisions.append(current_revision)
                identity_wrapper["compatible_runner_revisions"] = revisions
                identity_wrapper["active_artifact_root"] = str(artifact_root)
                _write_json(identity_path, identity_wrapper)
            else:
                raise RuntimeError(
                    "fallback experiment fingerprint changed; choose a new "
                    "--output-root")
    else:
        _write_json(identity_path, {
            "schema_version": 1,
            "experiment_identity": identity,
            "source_frozen_fingerprint": source_fingerprint,
            "current_fingerprint": current_fingerprint,
            "git_commit": _git_commit(project_dir),
            "recorded_at": _utc_now(),
        })

    projections = [
        _projection_for_group(full_root, case, target)
        for case in cases for target in targets
    ]
    projections.sort(key=_task_priority)
    selected = [
        projection for projection in projections
        if not args.degraded_only
        or projection["makespan_degraded"]
        or projection["movement_only_degraded"]
    ]
    preflight_failures = _preflight(
        full_root=full_root,
        candidate_root=candidate_root,
        cache_dir=cache_dir,
        cases=cases,
        targets=targets,
    )
    _write_json(output_root / "input_preflight.json", {
        "schema_version": 1,
        "status": "success" if not preflight_failures else "failed",
        "checked_case_count": len(cases),
        "checked_targets": list(targets),
        "failure_count": len(preflight_failures),
        "failures": preflight_failures,
        "timestamp": _utc_now(),
    })
    if preflight_failures:
        raise RuntimeError(
            "{} inherited-fallback inputs are missing or mismatched; see {}".format(
                len(preflight_failures), output_root / "input_preflight.json"))
    _write_json(output_root / "pre_reevaluation_projections.json", {
        "schema_version": 1,
        "value_kind": "pre_reevaluation_inference_not_official_measurement",
        "groups": projections,
        "priority_order": [
            {"case": row["case"], "target_cores": row["target_cores"]}
            for row in selected
        ],
        "timestamp": _utc_now(),
    })
    _write_json(output_root / "run_request.json", {
        "schema_version": 1,
        "cases": cases,
        "targets": list(targets),
        "workers": args.workers,
        "degraded_only": args.degraded_only,
        "artifact_root": str(artifact_root),
        "current_fallback_runner_sha256": computed_identity[
            "fallback_runner_sha256"],
        "compatible_runner_update": compatible_runner_update,
        "selected_task_count": len(selected),
        "makespan_degraded_task_count": sum(
            row["makespan_degraded"] for row in projections),
        "movement_only_degraded_task_count": sum(
            row["movement_only_degraded"] for row in projections),
        "experiment_identity": identity,
        "requested_at": _utc_now(),
    })
    common = {
        "full_root": str(full_root),
        "candidate_root": str(candidate_root),
        "cache_dir": str(cache_dir),
        "output_root": str(output_root),
        "data_dir": str(data_dir),
        "config_path": str(config_path),
        "experiment_identity": identity,
    }
    tasks = [
        {
            **common,
            "case": projection["case"],
            "target_cores": projection["target_cores"],
        }
        for projection in selected
    ]
    completed = 0
    if tasks:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            future_map = {executor.submit(_worker, task): task for task in tasks}
            for future in as_completed(future_map):
                task = future_map[future]
                try:
                    record = future.result()
                except Exception as error:
                    record = {
                        "status": "failed",
                        "case": task["case"],
                        "target_cores": task["target_cores"],
                        "error": str(error),
                    }
                completed += 1
                print(
                    "[{}/{}] {} k{} status={} final={} score=({}, {}) "
                    "improvement={} disposition={}".format(
                        completed, len(tasks), record.get("case"),
                        record.get("target_cores"), record.get("status"),
                        (record.get("final_source") or {}).get(
                            "name", (record.get("final_source") or {}).get(
                                "kind")),
                        record.get("final_makespan"),
                        record.get("final_added_copy_bytes"),
                        record.get("makespan_improvement"),
                        record.get("run_disposition"),
                    ),
                    flush=True,
                )
                _write_json(output_root / "latest_progress.json", {
                    "completed_tasks_this_invocation": completed,
                    "total_tasks_this_invocation": len(tasks),
                    "last_record": record,
                    "updated_at": _utc_now(),
                })
    return _summarize(
        full_root=full_root,
        output_root=output_root,
        cases=cases,
        targets=targets,
        source_fingerprint=source_fingerprint,
        current_fingerprint=current_fingerprint,
        experiment_identity=identity,
        workers=args.workers,
        invocation_wall=time.perf_counter() - invocation_started,
        degraded_only=args.degraded_only,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resumable official re-evaluation of lower-core winner fallbacks"))
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--config", default="data/config.txt")
    parser.add_argument(
        "--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT),
        help=(
            "base directory for experiment artifacts; defaults to "
            "PROBLEM1_ARTIFACT_ROOT or /media/data/yn"),
    )
    parser.add_argument(
        "--full-root",
        help="override read-only frozen full-run directory")
    parser.add_argument(
        "--candidate-output-root",
        help="override read-only original six-candidate plan directory")
    parser.add_argument(
        "--cache-dir", help="override shared official-evaluation cache")
    parser.add_argument(
        "--output-root", help="override inherited-fallback output directory")
    parser.add_argument("--cases", nargs="+", help="optional case subset")
    parser.add_argument(
        "--targets", nargs="+", type=int, default=list(TARGET_CORES))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--degraded-only", action="store_true",
        help=(
            "first pass: evaluate only groups whose old score is worse than "
            "a lower-core winner; rerun without this flag to complete all groups"),
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    try:
        summary = run_experiment(args)
    except (CandidateManagerError, FileNotFoundError, RuntimeError, ValueError) as error:
        print("[INHERITED FALLBACK ERROR] {}".format(error))
        return 2
    path = (Path(summary["output_root"])
            / "problem1_inherited_fallback_summary.json")
    print("summary: {}".format(path))
    print("complete: {}".format(summary["complete"]))
    print("failures: {}".format(summary["failure_count"]))
    print(
        "measured fallback candidates: {} projection mismatches: {}".format(
            summary["candidate_with_official_measurement_count"],
            summary["projection_mismatch_candidate_count"],
        ))
    if summary["failure_count"]:
        return 1
    if not args.degraded_only and not summary["complete"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "pad_plan_for_target",
    "select_final_option",
    "run_experiment",
]
