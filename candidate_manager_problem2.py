"""Content-addressed candidate management for Problem 2 (Scene B).

The official evaluator remains the only scoring authority.  This module owns
only candidate construction, strict identity tracking, deduplication, failure
isolation, timing, metric extraction, and resumable caching.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import platform
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple

from solver_problem1 import (
    OFFICIAL_CODE_DIR,
    GraphInfo,
    generate_problem1_plan_from_graph_info,
    generate_single_plan_from_graph_info,
)

from candidate_manager_problem1 import canonical_plan_signature, sha256_file
from contest_io import _read_json
from evaluation_validation import (
    read_evaluation_config,
    validate_task_order,
)
from multicore_cut_evaluate_problem_2 import evaluate_scene_b, read_scene_b_config
from stub_multicore_cut_and_schedule import derive_multicore_plan


SCHEMA_VERSION = 1
IMPLEMENTATION_VERSION = "problem2-stage1-baseline-v1"
ORIGINAL_CANDIDATES = (
    "single",
    "b0",
    "b1",
    "b2a_w4",
    "b2a_w8",
    "b2a_w16",
)
OFFICIAL_SCENE_B_FILES = (
    "contest_io.py",
    "evaluation_validation.py",
    "multicore_cut_evaluate_problem_1.py",
    "multicore_cut_evaluate_problem_2.py",
    "schedule_step1.py",
    "schedule_step2.py",
    "schedule_step3.py",
    "stub_multicore_cut_and_schedule.py",
)


class Problem2CandidateError(RuntimeError):
    """A Problem-2 group cannot produce a trustworthy winner."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def json_sha256(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value))


def _write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def official_scene_b_hash(
    official_code_dir: Path = OFFICIAL_CODE_DIR,
) -> Tuple[str, Dict[str, str]]:
    """Hash every repository source file imported by the Scene-B evaluator."""

    file_hashes: Dict[str, str] = {}
    digest = hashlib.sha256()
    for filename in OFFICIAL_SCENE_B_FILES:
        path = Path(official_code_dir) / filename
        if not path.is_file():
            raise Problem2CandidateError(
                "official Scene-B dependency is missing: {}".format(path))
        content_hash = sha256_file(path)
        file_hashes[filename] = content_hash
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest(), file_hashes


def problem2_implementation_hash() -> Tuple[str, Dict[str, str]]:
    """Hash the implementation that interprets and caches Scene-B results."""

    files = {
        "candidate_manager_problem2.py": sha256_file(Path(__file__).resolve()),
    }
    digest = hashlib.sha256()
    digest.update(IMPLEMENTATION_VERSION.encode("utf-8"))
    digest.update(b"\0")
    for name, content_hash in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest(), files


def evaluation_key(
    *,
    graph_hash: str,
    config_hash: str,
    plan_hash: str,
    canonical_signature: str,
    evaluator_hash: str,
    solver_hash: str,
    implementation_hash: str,
) -> str:
    """Return the complete scientific cache identity for one evaluation."""

    return json_sha256({
        "graph_hash": graph_hash,
        "config_hash": config_hash,
        "plan_hash": plan_hash,
        "canonical_signature": canonical_signature,
        "evaluator_hash": evaluator_hash,
        "solver_hash": solver_hash,
        "implementation_hash": implementation_hash,
    })


def pad_plan_with_empty_cores(
    source_plan: Mapping[str, Any], target_cores: int,
) -> Dict[str, Any]:
    """Preserve a lower-core plan and append only empty core schedules."""

    if set(source_plan) != {"node_to_subgraph", "core_schedules"}:
        raise Problem2CandidateError(
            "source plan must contain exactly the two official fields")
    mapping = source_plan["node_to_subgraph"]
    schedules = source_plan["core_schedules"]
    if not isinstance(mapping, Mapping) or not isinstance(schedules, list):
        raise Problem2CandidateError("source plan has invalid official fields")
    if any(not isinstance(schedule, list) for schedule in schedules):
        raise Problem2CandidateError("source core schedules must be lists")
    if (not isinstance(target_cores, int) or isinstance(target_cores, bool)
            or target_cores < len(schedules)):
        raise Problem2CandidateError(
            "target core count cannot be smaller than source core count")
    return {
        "node_to_subgraph": copy.deepcopy(dict(mapping)),
        "core_schedules": copy.deepcopy(schedules)
        + [[] for _ in range(target_cores - len(schedules))],
    }


def _merged_interval_length(intervals: Iterable[Tuple[int, int]]) -> int:
    ordered = sorted((int(start), int(end)) for start, end in intervals
                     if int(end) > int(start))
    if not ordered:
        return 0
    total = 0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + end - start


def per_core_utilization(result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Measure active time and per-pipe occupancy on the official timeline."""

    makespan = result.get("makespan")
    if (not isinstance(makespan, int) or isinstance(makespan, bool)
            or makespan < 0):
        raise Problem2CandidateError("official result has invalid makespan")
    timelines = result.get("per_core_timeline")
    if not isinstance(timelines, list):
        raise Problem2CandidateError("official result lacks per_core_timeline")
    records: List[Dict[str, Any]] = []
    for core in sorted(timelines, key=lambda item: int(item["core_id"])):
        ops = core.get("ops", [])
        if not isinstance(ops, list):
            raise Problem2CandidateError("official core timeline has invalid ops")
        intervals = [(op["start"], op["end"]) for op in ops]
        active_cycles = _merged_interval_length(intervals)
        pipe_busy: Dict[str, int] = {}
        for op in ops:
            pipe = str(op["pipe"])
            duration = int(op["end"]) - int(op["start"])
            pipe_busy[pipe] = pipe_busy.get(pipe, 0) + duration
        tasks = core.get("tasks", [])
        task_start = min((int(task["start"]) for task in tasks), default=0)
        task_end = max((int(task["end"]) for task in tasks), default=0)
        records.append({
            "core_id": int(core["core_id"]),
            "op_count": len(ops),
            "task_start": task_start,
            "task_end": task_end,
            "task_span_cycles": max(0, task_end - task_start),
            "active_any_pipe_cycles": active_cycles,
            "active_any_pipe_fraction": (
                active_cycles / makespan if makespan else 0.0),
            "pipe_busy_cycles": dict(sorted(pipe_busy.items())),
            "pipe_utilization": {
                pipe: (cycles / makespan if makespan else 0.0)
                for pipe, cycles in sorted(pipe_busy.items())
            },
        })
    return records


def validate_and_compact_result(result: Any) -> Dict[str, Any]:
    """Validate the official Scene-B schema and retain all requested metrics."""

    if not isinstance(result, Mapping):
        raise Problem2CandidateError("official evaluator did not return an object")
    if result.get("scene") != "B":
        raise Problem2CandidateError("official evaluator did not return Scene B")
    makespan = result.get("makespan")
    if (not isinstance(makespan, int) or isinstance(makespan, bool)
            or makespan < 0):
        raise Problem2CandidateError("official result has invalid makespan")
    movement = result.get("data_movement_bytes")
    if not isinstance(movement, Mapping):
        raise Problem2CandidateError("official result lacks data_movement_bytes")
    movement_fields = (
        "original_graph_copy_bytes",
        "scheduled_copy_bytes",
        "added_copy_bytes",
        "partition_added_copy_bytes",
        "spill_added_copy_bytes",
    )
    values: Dict[str, int] = {}
    for name in movement_fields:
        value = movement.get(name)
        if (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise Problem2CandidateError(
                "official result has invalid {}".format(name))
        values[name] = value
    cross_payload = result.get("cross_task_traffic")
    transfers = result.get("cross_core_transfers")
    if (not isinstance(cross_payload, int) or isinstance(cross_payload, bool)
            or cross_payload < 0):
        raise Problem2CandidateError("invalid cross_task_traffic")
    if not isinstance(transfers, list):
        raise Problem2CandidateError("invalid cross_core_transfers")
    utilization = per_core_utilization(result)
    memory_peak = result.get("memory_peak_by_core")
    if not isinstance(memory_peak, Mapping):
        raise Problem2CandidateError("official result lacks memory peaks")
    return {
        "makespan_cycles": makespan,
        **values,
        "cross_task_traffic_bytes": cross_payload,
        "cross_core_transfer_count": len(transfers),
        "used_core_count": sum(record["op_count"] > 0 for record in utilization),
        "memory_peak_by_core": copy.deepcopy(dict(memory_peak)),
        "per_core_utilization": utilization,
    }


class SceneBEvaluationCache:
    """Portable, content-addressed cache for successful Scene-B evaluations."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.results_dir = self.root / "results"

    def lookup(self, identity: Mapping[str, str]) -> Dict[str, Any] | None:
        key = identity["evaluation_key"]
        entry_path = self.entries_dir / (key + ".json")
        if not entry_path.is_file():
            return None
        try:
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
            if not isinstance(entry, dict) or entry.get("status") != "success":
                return None
            for name, value in identity.items():
                if entry.get(name) != value:
                    return None
            stored_path = Path(str(entry.get("result_path", "")))
            result_path = (stored_path if stored_path.is_absolute()
                           else self.root / stored_path)
            if not result_path.is_file():
                result_path = self.results_dir / (key + ".json")
            if not result_path.is_file():
                return None
            if sha256_file(result_path) != entry.get("result_file_sha256"):
                return None
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if json_sha256(result) != entry.get("result_json_sha256"):
                return None
            found = dict(entry)
            found["result"] = result
            found["resolved_result_path"] = str(result_path.resolve())
            return found
        except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
            return None

    def store(
        self,
        identity: Mapping[str, str],
        result: Mapping[str, Any],
        metrics: Mapping[str, Any],
        official_evaluation_seconds: float,
        metadata: Mapping[str, Any],
    ) -> Dict[str, Any]:
        key = identity["evaluation_key"]
        result_path = self.results_dir / (key + ".json")
        _write_json(result_path, result)
        entry = {
            **dict(identity),
            "status": "success",
            "result_path": str(Path("results") / (key + ".json")),
            "result_file_sha256": sha256_file(result_path),
            "result_json_sha256": json_sha256(result),
            "official_evaluation_seconds": official_evaluation_seconds,
            "metrics": copy.deepcopy(dict(metrics)),
            "metadata": copy.deepcopy(dict(metadata)),
            "timestamp": _utc_now(),
        }
        _write_json(self.entries_dir / (key + ".json"), entry)
        found = dict(entry)
        found["result"] = copy.deepcopy(dict(result))
        found["resolved_result_path"] = str(result_path.resolve())
        return found


@dataclass(frozen=True)
class InheritedCandidateSource:
    source_cores: int
    source_plan: Mapping[str, Any]
    source_winner: str
    source_plan_path: str | None = None
    source_group_hash: str | None = None


@dataclass
class Problem2Candidate:
    name: str
    family: str
    parameters: Dict[str, Any]
    provenance: str
    plan: Dict[str, Any] | None = None
    plan_path: str | None = None
    plan_file_sha256: str | None = None
    plan_hash: str | None = None
    canonical_signature: str | None = None
    canonical_candidate: str | None = None
    equivalent_candidates: List[str] = field(default_factory=list)
    deduplicated: bool = False
    legal: bool = False
    status: str = "pending"
    failure_stage: str | None = None
    error_type: str | None = None
    error: str | None = None
    cache_hit: bool = False
    evaluation_key: str | None = None
    official_result_path: str | None = None
    official_result_file_sha256: str | None = None
    official_result_json_sha256: str | None = None
    metrics: Dict[str, Any] | None = None
    generation_seconds: float = 0.0
    validation_seconds: float = 0.0
    cache_lookup_seconds: float = 0.0
    official_evaluation_seconds: float = 0.0
    cold_cache_estimated_seconds: float = 0.0
    invocation_wall_seconds: float = 0.0

    def score(self) -> Tuple[int, int]:
        if self.metrics is None:
            raise Problem2CandidateError(
                "candidate {} has no official metrics".format(self.name))
        return (
            int(self.metrics["makespan_cycles"]),
            int(self.metrics["added_copy_bytes"]),
        )

    def record(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family,
            "parameters": copy.deepcopy(self.parameters),
            "provenance": self.provenance,
            "status": self.status,
            "legal": self.legal,
            "failure_stage": self.failure_stage,
            "error_type": self.error_type,
            "error": self.error,
            "plan_path": self.plan_path,
            "plan_file_sha256": self.plan_file_sha256,
            "plan_hash": self.plan_hash,
            "canonical_signature": self.canonical_signature,
            "canonical_candidate": self.canonical_candidate,
            "equivalent_candidates": list(self.equivalent_candidates),
            "deduplicated": self.deduplicated,
            "cache_hit": self.cache_hit,
            "evaluation_key": self.evaluation_key,
            "official_result_path": self.official_result_path,
            "official_result_file_sha256": self.official_result_file_sha256,
            "official_result_json_sha256": self.official_result_json_sha256,
            "metrics": copy.deepcopy(self.metrics),
            "timing": {
                "generation_seconds": self.generation_seconds,
                "validation_seconds": self.validation_seconds,
                "cache_lookup_seconds": self.cache_lookup_seconds,
                "official_evaluation_seconds": self.official_evaluation_seconds,
                "cold_cache_estimated_seconds": self.cold_cache_estimated_seconds,
                "invocation_wall_seconds": self.invocation_wall_seconds,
            },
        }


@dataclass(frozen=True)
class Problem2GroupResult:
    winner_name: str
    winner_plan: Dict[str, Any]
    winner_metrics: Dict[str, Any]
    group_path: Path
    final_plan_path: Path
    manifest: Dict[str, Any]


def evaluate_official_problem2(
    graph_json: Mapping[str, Any],
    plan: Mapping[str, Any],
    config_path: Path,
) -> Dict[str, Any]:
    settings = read_evaluation_config(str(config_path))
    scene = read_scene_b_config(str(config_path))
    return evaluate_scene_b(
        graph_json,
        plan,
        bandwidth=settings["bandwidth"],
        capacity=settings["capacity"],
        cross_core_copy_delay=scene["cross_core_copy_delay_cycles"],
    )


def _original_specs() -> Tuple[Tuple[str, str, Dict[str, Any]], ...]:
    return (
        ("single", "SINGLE", {}),
        ("b0", "B0", {}),
        ("b1", "B1", {}),
        ("b2a_w4", "B2A", {"windows": 4}),
        ("b2a_w8", "B2A", {"windows": 8}),
        ("b2a_w16", "B2A", {"windows": 16}),
    )


def _mark_failure(
    candidate: Problem2Candidate, stage: str, error: BaseException,
) -> None:
    candidate.status = "failed"
    candidate.failure_stage = stage
    candidate.error_type = type(error).__name__
    candidate.error = "{}: {}".format(type(error).__name__, error)


def _copy_evaluation(
    source: Problem2Candidate, target: Problem2Candidate,
) -> None:
    target.status = source.status
    target.failure_stage = source.failure_stage
    target.error_type = source.error_type
    target.error = source.error
    target.cache_hit = source.cache_hit
    target.evaluation_key = source.evaluation_key
    target.official_result_path = source.official_result_path
    target.official_result_file_sha256 = source.official_result_file_sha256
    target.official_result_json_sha256 = source.official_result_json_sha256
    target.metrics = copy.deepcopy(source.metrics)
    target.cache_lookup_seconds = source.cache_lookup_seconds
    target.official_evaluation_seconds = source.official_evaluation_seconds
    target.cold_cache_estimated_seconds = (
        target.generation_seconds + target.validation_seconds
        + source.official_evaluation_seconds)


def run_problem2_candidate_group(
    graph_path: Path,
    num_cores: int,
    *,
    config_path: Path,
    output_root: Path,
    cache_dir: Path,
    inherited_sources: Sequence[InheritedCandidateSource] = (),
    evaluator: Callable[[Mapping[str, Any], Mapping[str, Any], Path],
                        Dict[str, Any]] = evaluate_official_problem2,
    evaluator_hash_override: str | None = None,
    implementation_hash_override: str | None = None,
) -> Problem2GroupResult:
    """Generate, validate, evaluate, and select one case/core candidate group."""

    group_started = time.perf_counter()
    graph_path = Path(graph_path).resolve()
    config_path = Path(config_path).resolve()
    output_root = Path(output_root).resolve()
    cache_dir = Path(cache_dir).resolve()
    if not graph_path.is_file():
        raise FileNotFoundError("graph not found: {}".format(graph_path))
    if not config_path.is_file():
        raise FileNotFoundError("config not found: {}".format(config_path))
    if (not isinstance(num_cores, int) or isinstance(num_cores, bool)
            or num_cores < 1):
        raise Problem2CandidateError("num_cores must be a positive integer")

    case = graph_path.stem
    run_dir = output_root / "groups" / case / "k{}".format(num_cores)
    plans_dir = run_dir / "plans"
    records_dir = run_dir / "candidate_records"
    official_results_dir = run_dir / "official_results"
    graph_json = _read_json(graph_path)
    graph_hash = sha256_file(graph_path)
    config_hash = sha256_file(config_path)
    solver_path = Path(__file__).resolve().parent / "solver_problem1.py"
    solver_hash = sha256_file(solver_path)
    evaluator_hash, evaluator_files = official_scene_b_hash()
    if evaluator_hash_override is not None:
        evaluator_hash = evaluator_hash_override
        evaluator_files = {"override": evaluator_hash_override}
    implementation_hash, implementation_files = problem2_implementation_hash()
    if implementation_hash_override is not None:
        implementation_hash = implementation_hash_override
        implementation_files = {"override": implementation_hash_override}
    identity = {
        "graph_sha256": graph_hash,
        "config_sha256": config_hash,
        "solver_sha256": solver_hash,
        "evaluator_sha256": evaluator_hash,
        "problem2_implementation_sha256": implementation_hash,
    }
    feature_started = time.perf_counter()
    graph_info = GraphInfo.from_graph(graph_json)
    feature_seconds = time.perf_counter() - feature_started

    candidates: List[Problem2Candidate] = []
    builders: List[Tuple[Problem2Candidate, Callable[[], Dict[str, Any]]]] = []
    for name, method, parameters in _original_specs():
        candidate = Problem2Candidate(
            name=name,
            family="original_problem1",
            parameters=dict(parameters),
            provenance="generated_by_current_problem1_solver",
        )

        def build_original(
            method: str = method, parameters: Dict[str, Any] = dict(parameters),
        ) -> Dict[str, Any]:
            if method == "SINGLE":
                return generate_single_plan_from_graph_info(graph_info, num_cores)
            plan, _ = generate_problem1_plan_from_graph_info(
                graph_info,
                num_cores,
                method=method,
                windows=parameters.get("windows"),
            )
            return plan

        candidates.append(candidate)
        builders.append((candidate, build_original))

    for source in sorted(inherited_sources, key=lambda item: item.source_cores):
        if source.source_cores < 2 or source.source_cores >= num_cores:
            raise Problem2CandidateError(
                "inherited source cores must satisfy 2 <= source < target")
        candidate = Problem2Candidate(
            name="inherit_k{}".format(source.source_cores),
            family="problem2_lower_core_winner_inheritance",
            parameters={
                "source_cores": source.source_cores,
                "source_winner": source.source_winner,
                "source_plan_path": source.source_plan_path,
                "source_group_hash": source.source_group_hash,
                "padding": "append_empty_core_schedules",
            },
            provenance="official_scene_b_lower_core_winner",
        )

        def build_inherited(source: InheritedCandidateSource = source) -> Dict[str, Any]:
            return pad_plan_with_empty_cores(source.source_plan, num_cores)

        candidates.append(candidate)
        builders.append((candidate, build_inherited))

    for candidate, builder in builders:
        candidate_started = time.perf_counter()
        generation_started = time.perf_counter()
        try:
            candidate.plan = builder()
            candidate.status = "generated"
            candidate.generation_seconds = time.perf_counter() - generation_started
            plan_path = plans_dir / (candidate.name + "_multicore_res.json")
            _write_json(plan_path, candidate.plan)
            candidate.plan_path = str(plan_path.resolve())
            candidate.plan_file_sha256 = sha256_file(plan_path)
            candidate.plan_hash = json_sha256(candidate.plan)
        except Exception as error:
            candidate.generation_seconds = time.perf_counter() - generation_started
            _mark_failure(candidate, "generation", error)
            candidate.invocation_wall_seconds = time.perf_counter() - candidate_started
            continue

        validation_started = time.perf_counter()
        try:
            view = derive_multicore_plan(graph_json, candidate.plan)
            validate_task_order(view)
            candidate.canonical_signature = canonical_plan_signature(candidate.plan)
            candidate.legal = True
            candidate.status = "valid"
        except Exception as error:
            _mark_failure(candidate, "validation", error)
        candidate.validation_seconds = time.perf_counter() - validation_started
        candidate.invocation_wall_seconds = time.perf_counter() - candidate_started

    representatives: Dict[str, Problem2Candidate] = {}
    groups: Dict[str, List[Problem2Candidate]] = {}
    for candidate in candidates:
        if not candidate.legal or candidate.canonical_signature is None:
            continue
        signature = candidate.canonical_signature
        group = groups.setdefault(signature, [])
        group.append(candidate)
        if signature not in representatives:
            representatives[signature] = candidate
            candidate.canonical_candidate = candidate.name
        else:
            candidate.deduplicated = True
            candidate.canonical_candidate = representatives[signature].name
    for group in groups.values():
        names = [candidate.name for candidate in group]
        for candidate in group:
            candidate.equivalent_candidates = list(names)

    cache = SceneBEvaluationCache(cache_dir)
    official_evaluations = 0
    cache_hits = 0
    for signature, representative in representatives.items():
        evaluation_started_wall = time.perf_counter()
        assert representative.plan is not None
        assert representative.plan_hash is not None
        key = evaluation_key(
            graph_hash=graph_hash,
            config_hash=config_hash,
            plan_hash=representative.plan_hash,
            canonical_signature=signature,
            evaluator_hash=evaluator_hash,
            solver_hash=solver_hash,
            implementation_hash=implementation_hash,
        )
        representative.evaluation_key = key
        cache_identity = {
            "evaluation_key": key,
            "graph_sha256": graph_hash,
            "config_sha256": config_hash,
            "plan_sha256": representative.plan_hash,
            "canonical_signature": signature,
            "evaluator_sha256": evaluator_hash,
            "solver_sha256": solver_hash,
            "problem2_implementation_sha256": implementation_hash,
        }
        lookup_started = time.perf_counter()
        entry = cache.lookup(cache_identity)
        representative.cache_lookup_seconds = time.perf_counter() - lookup_started
        try:
            if entry is None:
                official_evaluations += 1
                official_started = time.perf_counter()
                result = evaluator(graph_json, representative.plan, config_path)
                representative.official_evaluation_seconds = (
                    time.perf_counter() - official_started)
                metrics = validate_and_compact_result(result)
                entry = cache.store(
                    cache_identity,
                    result,
                    metrics,
                    representative.official_evaluation_seconds,
                    metadata={
                        "case": case,
                        "cores": num_cores,
                        "candidate": representative.name,
                    },
                )
            else:
                cache_hits += 1
                representative.cache_hit = True
                result = entry["result"]
                metrics = validate_and_compact_result(result)
                representative.official_evaluation_seconds = float(
                    entry["official_evaluation_seconds"])
            official_result_path = (
                official_results_dir / (representative.name + ".json"))
            _write_json(official_result_path, result)
            representative.official_result_path = str(official_result_path.resolve())
            representative.official_result_file_sha256 = sha256_file(
                official_result_path)
            representative.official_result_json_sha256 = json_sha256(result)
            representative.metrics = metrics
            representative.status = "evaluated"
            representative.cold_cache_estimated_seconds = (
                representative.generation_seconds
                + representative.validation_seconds
                + representative.official_evaluation_seconds)
        except Exception as error:
            _mark_failure(representative, "evaluation", error)
        representative.invocation_wall_seconds += (
            time.perf_counter() - evaluation_started_wall)

        for alias in groups[signature]:
            if alias is representative:
                continue
            _copy_evaluation(representative, alias)

    priority = {candidate.name: index for index, candidate in enumerate(candidates)}
    successful = [
        candidate for candidate in candidates
        if candidate.status == "evaluated" and candidate.metrics is not None
    ]
    single = next(candidate for candidate in candidates if candidate.name == "single")
    fatal_error: str | None = None
    winner: Problem2Candidate | None = None
    if single.status != "evaluated":
        fatal_error = "single safety candidate failed: {}".format(single.error)
    elif not successful:
        fatal_error = "no candidate completed official Scene-B evaluation"
    elif num_cores == 1:
        # The problem statement defines T_i,1 on the unpartitioned whole graph.
        # Other one-core partitions remain recorded as diagnostics, but they do
        # not replace the official single-core speedup denominator.
        winner = single
    else:
        winner = min(
            successful,
            key=lambda candidate: (*candidate.score(), priority[candidate.name]),
        )

    candidate_record_file_hashes: Dict[str, str] = {}
    for candidate in candidates:
        record_path = records_dir / (candidate.name + ".json")
        _write_json(record_path, {
            "schema_version": SCHEMA_VERSION,
            "problem": 2,
            "case": case,
            "cores": num_cores,
            **identity,
            "candidate": candidate.record(),
            "timestamp": _utc_now(),
        })
        candidate_record_file_hashes[candidate.name] = sha256_file(record_path)

    final_plan_path = run_dir / "final_multicore_res.json"
    final_result_path = run_dir / "final_official_evaluation.json"
    if winner is not None and winner.plan is not None and fatal_error is None:
        _write_json(final_plan_path, winner.plan)
        if winner.official_result_path is not None:
            final_result = json.loads(
                Path(winner.official_result_path).read_text(encoding="utf-8"))
            _write_json(final_result_path, final_result)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "problem2_candidate_group",
        "problem": 2,
        "status": "failed" if fatal_error else "success",
        "case": case,
        "cores": num_cores,
        "graph_path": str(graph_path),
        "config_path": str(config_path),
        **identity,
        "evaluator_file_sha256": evaluator_files,
        "implementation_file_sha256": implementation_files,
        "implementation_version": IMPLEMENTATION_VERSION,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "candidate_priority": [candidate.name for candidate in candidates],
        "generated_candidates": len(candidates),
        "legal_candidates": sum(candidate.legal for candidate in candidates),
        "unique_legal_plans": len(representatives),
        "official_evaluations": official_evaluations,
        "cache_hits": cache_hits,
        "failed_candidates": [
            candidate.name for candidate in candidates
            if candidate.status == "failed"
        ],
        "winner": None if winner is None else {
            "name": winner.name,
            "score": list(winner.score()),
            "metrics": copy.deepcopy(winner.metrics),
            "canonical_signature": winner.canonical_signature,
            "plan_hash": winner.plan_hash,
        },
        "final_plan_path": str(final_plan_path.resolve()),
        "final_plan_file_sha256": (
            sha256_file(final_plan_path) if final_plan_path.is_file() else None),
        "final_official_result_path": str(final_result_path.resolve()),
        "final_official_result_file_sha256": (
            sha256_file(final_result_path) if final_result_path.is_file() else None),
        "final_official_result_json_sha256": (
            json_sha256(json.loads(final_result_path.read_text(encoding="utf-8")))
            if final_result_path.is_file() else None),
        "candidate_record_file_sha256": candidate_record_file_hashes,
        "fatal_error": fatal_error,
        "timing": {
            "feature_seconds": feature_seconds,
            "candidate_generation_seconds": sum(
                item.generation_seconds for item in candidates),
            "validation_seconds": sum(
                item.validation_seconds for item in candidates),
            "official_evaluation_seconds": sum(
                item.official_evaluation_seconds
                for item in representatives.values()),
            "cold_cache_estimated_seconds": (
                feature_seconds + sum(
                    item.cold_cache_estimated_seconds
                    for item in representatives.values())),
            "invocation_wall_seconds": time.perf_counter() - group_started,
        },
        "candidates": [candidate.record() for candidate in candidates],
        "timestamp": _utc_now(),
    }
    group_path = output_root / "groups" / case / "k{}.json".format(num_cores)
    _write_json(group_path, manifest)
    if fatal_error is not None or winner is None or winner.plan is None:
        raise Problem2CandidateError(fatal_error or "candidate group failed")
    return Problem2GroupResult(
        winner_name=winner.name,
        winner_plan=copy.deepcopy(winner.plan),
        winner_metrics=copy.deepcopy(winner.metrics or {}),
        group_path=group_path,
        final_plan_path=final_plan_path,
        manifest=manifest,
    )


__all__ = [
    "IMPLEMENTATION_VERSION",
    "InheritedCandidateSource",
    "OFFICIAL_SCENE_B_FILES",
    "ORIGINAL_CANDIDATES",
    "Problem2CandidateError",
    "Problem2GroupResult",
    "SceneBEvaluationCache",
    "evaluate_official_problem2",
    "evaluation_key",
    "json_sha256",
    "official_scene_b_hash",
    "pad_plan_with_empty_cores",
    "per_core_utilization",
    "problem2_implementation_hash",
    "run_problem2_candidate_group",
    "validate_and_compact_result",
]
