"""Official-evaluator-driven candidate management for Problem 1.

The candidate pool is deliberately fixed.  This module does not create new
partitioning or scheduling heuristics; it generates Single/B0/B1/B2A plans,
deduplicates their concrete task layouts, evaluates unique plans with the
official Scene-A evaluator, and selects by official metrics only.
"""

from __future__ import annotations

import csv
import hashlib
import json
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

from evaluation_validation import read_evaluation_config, validate_task_order
from multicore_cut_evaluate_problem_1 import evaluate_scene_a, read_scene_a_config
from stub_multicore_cut_and_schedule import derive_multicore_plan
from contest_io import _read_json


CANDIDATE_PRIORITY = (
    "single",
    "b0",
    "b1",
    "b2a_w4",
    "b2a_w8",
    "b2a_w16",
)

OFFICIAL_EVALUATOR_FILES = (
    "contest_io.py",
    "evaluation_validation.py",
    "multicore_cut_evaluate_problem_1.py",
    "schedule_step1.py",
    "schedule_step2.py",
    "schedule_step3.py",
    "stub_multicore_cut_and_schedule.py",
)


class CandidateManagerError(RuntimeError):
    """The fixed candidate pool could not produce a trustworthy final plan."""


@dataclass
class CandidatePlan:
    name: str
    method: str
    parameters: Dict[str, Any]
    plan: Dict[str, Any] | None = None
    canonical_signature: str | None = None
    generation_time: float = 0.0
    validation_time: float = 0.0
    evaluation_time: float = 0.0
    cache_lookup_time: float = 0.0
    status: str = "pending"
    error: str | None = None
    canonical_candidate: str | None = None
    equivalent_methods: List[str] = field(default_factory=list)
    deduplicated: bool = False
    cache_hit: bool = False
    evaluation_key: str | None = None
    makespan: int | None = None
    added_copy_bytes: int | None = None
    max_memory_bytes: int | None = None
    full_result_path: str | None = None

    def score(self) -> Tuple[int, int]:
        if self.makespan is None or self.added_copy_bytes is None:
            raise CandidateManagerError(
                "candidate {} does not have official metrics".format(self.name))
        return self.makespan, self.added_copy_bytes

    def manifest_record(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "method": self.method,
            "parameters": dict(self.parameters),
            "status": self.status,
            "error": self.error,
            "canonical_signature": self.canonical_signature,
            "canonical_candidate": self.canonical_candidate,
            "equivalent_methods": list(self.equivalent_methods),
            "deduplicated": self.deduplicated,
            "generation_time": self.generation_time,
            "validation_time": self.validation_time,
            "evaluation_time": self.evaluation_time,
            "cache_lookup_time": self.cache_lookup_time,
            "cache_hit": self.cache_hit,
            "evaluation_key": self.evaluation_key,
            "makespan": self.makespan,
            "added_copy_bytes": self.added_copy_bytes,
            "max_memory_bytes": self.max_memory_bytes,
            "full_result_path": self.full_result_path,
        }


@dataclass(frozen=True)
class CandidateManagerResult:
    final_plan: Dict[str, Any]
    winner_name: str
    winner_makespan: int
    winner_added_copy_bytes: int
    manifest: Dict[str, Any]
    manifest_path: Path
    final_plan_path: Path


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def official_evaluator_hash(
    official_code_dir: Path = OFFICIAL_CODE_DIR,
) -> Tuple[str, Dict[str, str]]:
    """Hash the complete local source dependency set used by Scene A."""

    digest = hashlib.sha256()
    file_hashes: Dict[str, str] = {}
    for filename in OFFICIAL_EVALUATOR_FILES:
        path = Path(official_code_dir) / filename
        if not path.is_file():
            raise CandidateManagerError(
                "official evaluator dependency is missing: {}".format(path))
        content_hash = sha256_file(path)
        file_hashes[filename] = content_hash
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest(), file_hashes


def canonical_plan(plan: Mapping[str, Any]) -> Tuple[Tuple[Tuple[int, ...], ...], ...]:
    """Represent a plan independently of its numeric subgraph identifiers."""

    if set(plan) != {"node_to_subgraph", "core_schedules"}:
        raise CandidateManagerError("plan must contain exactly the two official fields")
    mapping_value = plan["node_to_subgraph"]
    schedules = plan["core_schedules"]
    if not isinstance(mapping_value, Mapping) or not isinstance(schedules, list):
        raise CandidateManagerError("invalid plan mapping or schedules")

    mapping: Dict[int, int] = {}
    for raw_node, subgraph_id in mapping_value.items():
        if (not isinstance(subgraph_id, int)
                or isinstance(subgraph_id, bool)):
            raise CandidateManagerError("subgraph ids must be integers")
        try:
            node_id = int(raw_node)
        except (TypeError, ValueError) as error:
            raise CandidateManagerError("op ids must be integers") from error
        if node_id in mapping:
            raise CandidateManagerError("duplicate integer op id in plan")
        mapping[node_id] = subgraph_id

    group_nodes: Dict[int, List[int]] = {}
    for node_id, subgraph_id in mapping.items():
        group_nodes.setdefault(subgraph_id, []).append(node_id)
    groups = {
        subgraph_id: tuple(sorted(node_ids))
        for subgraph_id, node_ids in group_nodes.items()
    }
    seen: List[int] = []
    canonical_cores: List[Tuple[Tuple[int, ...], ...]] = []
    for core_schedule in schedules:
        if not isinstance(core_schedule, list):
            raise CandidateManagerError("each core schedule must be a list")
        core_groups = []
        for subgraph_id in core_schedule:
            if subgraph_id not in groups:
                raise CandidateManagerError(
                    "scheduled subgraph has no mapped nodes")
            seen.append(subgraph_id)
            core_groups.append(groups[subgraph_id])
        canonical_cores.append(tuple(core_groups))
    if len(seen) != len(set(seen)) or set(seen) != set(groups):
        raise CandidateManagerError(
            "core schedules must cover each mapped subgraph exactly once")
    return tuple(canonical_cores)


def canonical_plan_signature(plan: Mapping[str, Any]) -> str:
    return _sha256_bytes(_json_bytes(canonical_plan(plan)))


def evaluation_key(
    graph_hash: str,
    config_hash: str,
    plan_hash: str,
    evaluator_hash: str,
) -> str:
    return _sha256_bytes(_json_bytes({
        "graph_hash": graph_hash,
        "config_hash": config_hash,
        "plan_hash": plan_hash,
        "evaluator_hash": evaluator_hash,
    }))


def _write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class EvaluationCache:
    """Strict content-addressed cache for successful official evaluations."""

    REQUIRED_FIELDS = {
        "evaluation_key",
        "graph_hash",
        "config_hash",
        "plan_hash",
        "evaluator_hash",
        "makespan",
        "added_copy_bytes",
        "full_result_path",
        "evaluation_time",
        "timestamp",
        "status",
    }

    def __init__(self, root: Path):
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.results_dir = self.root / "results"

    def lookup(
        self,
        graph_hash: str,
        config_hash: str,
        plan_hash: str,
        evaluator_hash: str,
    ) -> Dict[str, Any] | None:
        key = evaluation_key(
            graph_hash, config_hash, plan_hash, evaluator_hash)
        entry_path = self.entries_dir / (key + ".json")
        if not entry_path.is_file():
            return None
        try:
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
            if not isinstance(entry, dict):
                return None
            if not self.REQUIRED_FIELDS.issubset(entry):
                return None
            expected = {
                "evaluation_key": key,
                "graph_hash": graph_hash,
                "config_hash": config_hash,
                "plan_hash": plan_hash,
                "evaluator_hash": evaluator_hash,
                "status": "success",
            }
            if any(entry.get(name) != value for name, value in expected.items()):
                return None
            if (not isinstance(entry["makespan"], int)
                    or isinstance(entry["makespan"], bool)
                    or entry["makespan"] < 0):
                return None
            if (not isinstance(entry["added_copy_bytes"], int)
                    or isinstance(entry["added_copy_bytes"], bool)
                    or entry["added_copy_bytes"] < 0):
                return None
            stored_result_path = Path(entry["full_result_path"])
            if stored_result_path.is_absolute():
                result_path = stored_result_path
            else:
                result_path = self.root / stored_result_path
            if not result_path.is_file():
                # Compatibility with entries created before cache paths became
                # portable: after copying the cache to another machine, the
                # old absolute path is stale but the content-addressed result
                # still lives at the deterministic location below.
                result_path = self.results_dir / (key + ".json")
            if not result_path.is_file():
                return None
            portable_entry = dict(entry)
            portable_entry["full_result_path"] = str(result_path.resolve())
            return portable_entry
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def store(
        self,
        *,
        graph_hash: str,
        config_hash: str,
        plan_hash: str,
        evaluator_hash: str,
        result: Mapping[str, Any],
        evaluation_time: float,
        max_memory_bytes: int,
        metadata: Mapping[str, Any],
    ) -> Tuple[Dict[str, Any], float]:
        started = time.perf_counter()
        key = evaluation_key(
            graph_hash, config_hash, plan_hash, evaluator_hash)
        result_path = (self.results_dir / (key + ".json")).resolve()
        _write_json(result_path, result)
        movement = result["data_movement_bytes"]
        entry = {
            "evaluation_key": key,
            "graph_hash": graph_hash,
            "config_hash": config_hash,
            "plan_hash": plan_hash,
            "evaluator_hash": evaluator_hash,
            "makespan": result["makespan"],
            "added_copy_bytes": movement["added_copy_bytes"],
            "max_memory_bytes": max_memory_bytes,
            "full_result_path": str(Path("results") / (key + ".json")),
            "evaluation_time": evaluation_time,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "success",
            "metadata": dict(metadata),
        }
        _write_json(self.entries_dir / (key + ".json"), entry)
        return entry, time.perf_counter() - started


def _validate_official_result(result: Any) -> Tuple[int, int, int]:
    if not isinstance(result, Mapping):
        raise CandidateManagerError("official evaluator did not return an object")
    makespan = result.get("makespan")
    movement = result.get("data_movement_bytes")
    if (not isinstance(makespan, int) or isinstance(makespan, bool)
            or makespan < 0):
        raise CandidateManagerError("official result has invalid makespan")
    if not isinstance(movement, Mapping):
        raise CandidateManagerError("official result lacks data_movement_bytes")
    added = movement.get("added_copy_bytes")
    if (not isinstance(added, int) or isinstance(added, bool) or added < 0):
        raise CandidateManagerError("official result has invalid added_copy_bytes")
    memory = result.get("memory_peak_by_core", {})
    peaks = [
        value
        for core_peaks in memory.values()
        if isinstance(core_peaks, Mapping)
        for value in core_peaks.values()
        if isinstance(value, int) and not isinstance(value, bool)
    ] if isinstance(memory, Mapping) else []
    return makespan, added, max(peaks, default=0)


def evaluate_official_problem1(
    graph_json: Mapping[str, Any],
    plan: Mapping[str, Any],
    config_path: Path,
) -> Dict[str, Any]:
    settings = read_evaluation_config(str(config_path))
    scene = read_scene_a_config(str(config_path))
    return evaluate_scene_a(
        graph_json,
        plan,
        bandwidth=settings["bandwidth"],
        capacity=settings["capacity"],
        cross_core_wait=scene["task_cross_core_wait_cycles"],
        same_core_wait=scene["task_same_core_wait_cycles"],
    )


def deduplicate_candidates(
    candidates: Sequence[CandidatePlan],
) -> List[CandidatePlan]:
    """Group valid candidates by canonical plan, preserving fixed priority."""

    representatives: Dict[str, CandidatePlan] = {}
    groups: Dict[str, List[CandidatePlan]] = {}
    for candidate in candidates:
        if candidate.status != "valid" or candidate.canonical_signature is None:
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
    for signature, group in groups.items():
        names = [candidate.name for candidate in group]
        for candidate in group:
            candidate.equivalent_methods = list(names)
    return list(representatives.values())


def select_best_candidate(candidates: Sequence[CandidatePlan]) -> CandidatePlan:
    priority = {name: index for index, name in enumerate(CANDIDATE_PRIORITY)}
    eligible = [candidate for candidate in candidates
                if candidate.status == "evaluated"
                and candidate.makespan is not None
                and candidate.added_copy_bytes is not None]
    if not eligible:
        raise CandidateManagerError("no valid candidate completed official evaluation")
    return min(
        eligible,
        key=lambda candidate: (
            candidate.makespan,
            candidate.added_copy_bytes,
            priority[candidate.name],
        ),
    )


def _candidate_specs() -> Tuple[Tuple[str, str, Dict[str, Any]], ...]:
    return (
        ("single", "SINGLE", {}),
        ("b0", "B0", {}),
        ("b1", "B1", {}),
        ("b2a_w4", "B2A", {"windows": 4}),
        ("b2a_w8", "B2A", {"windows": 8}),
        ("b2a_w16", "B2A", {"windows": 16}),
    )


def _candidate_log(candidate: CandidatePlan) -> str:
    if candidate.status == "evaluated":
        if candidate.deduplicated:
            source = "same plan as {}".format(candidate.canonical_candidate)
        elif candidate.cache_hit:
            source = "cache hit"
        else:
            source = "official evaluation"
        return (
            "{}: makespan={} added_copy_bytes={} source={}\n".format(
                candidate.name, candidate.makespan,
                candidate.added_copy_bytes, source))
    return "{}: status={} error={}\n".format(
        candidate.name, candidate.status, candidate.error)


def _write_candidate_summary(path: Path, candidates: Sequence[CandidatePlan]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fields = [
        "name", "method", "status", "canonical_candidate", "deduplicated",
        "equivalent_methods", "canonical_signature", "cache_hit", "makespan",
        "added_copy_bytes", "max_memory_bytes", "generation_time",
        "validation_time", "cache_lookup_time", "evaluation_time", "error",
    ]
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for candidate in candidates:
            record = candidate.manifest_record()
            record["equivalent_methods"] = ";".join(
                candidate.equivalent_methods)
            writer.writerow({field: record.get(field) for field in fields})
    temporary.replace(path)


def run_candidate_manager(
    graph_path: Path,
    num_cores: int,
    *,
    config_path: Path | None = None,
    output_root: Path | None = None,
    cache_dir: Path | None = None,
    evaluator: Callable[[Mapping[str, Any], Mapping[str, Any], Path],
                        Dict[str, Any]] = evaluate_official_problem1,
) -> CandidateManagerResult:
    """Generate, deduplicate, officially evaluate, and select the fixed pool."""

    total_started = time.perf_counter()
    graph_path = Path(graph_path).resolve()
    config_path = Path(config_path or graph_path.parent / "config.txt").resolve()
    if not graph_path.is_file():
        raise FileNotFoundError("graph not found: {}".format(graph_path))
    if not config_path.is_file():
        raise FileNotFoundError("config not found: {}".format(config_path))
    if not isinstance(num_cores, int) or isinstance(num_cores, bool) or num_cores < 1:
        raise CandidateManagerError("num_cores must be a positive integer")

    project_dir = Path(__file__).resolve().parent
    output_root = Path(
        output_root or project_dir / "artifacts" / "problem1_candidates")
    cache_dir = Path(
        cache_dir or project_dir / "artifacts" / "problem1_candidate_cache")
    run_dir = output_root / graph_path.stem / ("k{}".format(num_cores))
    candidates_dir = run_dir / "candidates"
    results_dir = run_dir / "results"
    logs_dir = run_dir / "logs"
    cache = EvaluationCache(cache_dir)
    output_time = 0.0

    graph_json = _read_json(graph_path)
    graph_hash = sha256_file(graph_path)
    config_hash = sha256_file(config_path)
    evaluator_hash, evaluator_file_hashes = official_evaluator_hash()

    feature_started = time.perf_counter()
    graph_info = GraphInfo.from_graph(graph_json)
    feature_time = time.perf_counter() - feature_started

    candidates: List[CandidatePlan] = []
    for name, method, parameters in _candidate_specs():
        candidate = CandidatePlan(
            name=name, method=method, parameters=dict(parameters))
        candidates.append(candidate)
        generation_started = time.perf_counter()
        try:
            if method == "SINGLE":
                candidate.plan = generate_single_plan_from_graph_info(
                    graph_info, num_cores)
            else:
                candidate.plan, _ = generate_problem1_plan_from_graph_info(
                    graph_info, num_cores, method=method,
                    windows=parameters.get("windows"))
            candidate.status = "generated"
        except Exception as error:  # Candidate failures are isolated by design.
            candidate.status = "failed"
            candidate.error = "generation: {}".format(error)
        candidate.generation_time = time.perf_counter() - generation_started

        if candidate.plan is not None:
            write_started = time.perf_counter()
            _write_json(
                candidates_dir / (candidate.name + "_multicore_res.json"),
                candidate.plan)
            output_time += time.perf_counter() - write_started

        if candidate.status != "generated":
            continue
        validation_started = time.perf_counter()
        try:
            view = derive_multicore_plan(graph_json, candidate.plan)
            validate_task_order(view)
            candidate.canonical_signature = canonical_plan_signature(
                candidate.plan)
            candidate.status = "valid"
        except Exception as error:
            candidate.status = "failed"
            candidate.error = "validation: {}".format(error)
        candidate.validation_time = time.perf_counter() - validation_started

    representatives = deduplicate_candidates(candidates)
    groups = {
        representative.canonical_signature: [
            candidate for candidate in candidates
            if candidate.canonical_signature == representative.canonical_signature
        ]
        for representative in representatives
    }
    official_evaluations = 0
    cache_hits = 0
    for representative in representatives:
        signature = representative.canonical_signature
        if signature is None:
            continue
        representative.evaluation_key = evaluation_key(
            graph_hash, config_hash, signature, evaluator_hash)
        lookup_started = time.perf_counter()
        entry = cache.lookup(
            graph_hash, config_hash, signature, evaluator_hash)
        representative.cache_lookup_time = time.perf_counter() - lookup_started
        if entry is not None:
            representative.cache_hit = True
            cache_hits += 1
            representative.makespan = entry["makespan"]
            representative.added_copy_bytes = entry["added_copy_bytes"]
            representative.max_memory_bytes = entry.get("max_memory_bytes", 0)
            representative.full_result_path = entry["full_result_path"]
            representative.status = "evaluated"
        else:
            evaluation_started = time.perf_counter()
            official_evaluations += 1
            try:
                result = evaluator(graph_json, representative.plan, config_path)
                representative.evaluation_time = (
                    time.perf_counter() - evaluation_started)
                makespan, added, max_memory = _validate_official_result(result)
                representative.makespan = makespan
                representative.added_copy_bytes = added
                representative.max_memory_bytes = max_memory
                entry, cache_write_time = cache.store(
                    graph_hash=graph_hash,
                    config_hash=config_hash,
                    plan_hash=signature,
                    evaluator_hash=evaluator_hash,
                    result=result,
                    evaluation_time=representative.evaluation_time,
                    max_memory_bytes=max_memory,
                    metadata={
                        "graph": graph_path.name,
                        "candidate": representative.name,
                        "num_cores": num_cores,
                    },
                )
                output_time += cache_write_time
                representative.full_result_path = entry["full_result_path"]
                representative.status = "evaluated"
            except Exception as error:
                representative.evaluation_time = (
                    time.perf_counter() - evaluation_started)
                representative.status = "failed"
                representative.error = "evaluation: {}".format(error)

        for candidate in groups[signature]:
            if candidate is representative:
                continue
            candidate.evaluation_key = representative.evaluation_key
            candidate.status = representative.status
            candidate.error = representative.error
            candidate.makespan = representative.makespan
            candidate.added_copy_bytes = representative.added_copy_bytes
            candidate.max_memory_bytes = representative.max_memory_bytes
            candidate.full_result_path = representative.full_result_path

    single = candidates[0]
    winner: CandidatePlan | None = None
    fatal_error: str | None = None
    try:
        if single.status != "evaluated":
            raise CandidateManagerError(
                "Single safety candidate failed: {}".format(single.error))
        winner = select_best_candidate(candidates)
        for baseline_name in ("single", "b0"):
            baseline = next(candidate for candidate in candidates
                            if candidate.name == baseline_name)
            if (baseline.status == "evaluated"
                    and winner.score() > baseline.score()):
                raise CandidateManagerError(
                    "winner is worse than evaluated {}".format(baseline_name))
    except CandidateManagerError as error:
        fatal_error = str(error)

    for candidate in candidates:
        write_started = time.perf_counter()
        _write_json(
            results_dir / (candidate.name + "_evaluation.json"),
            candidate.manifest_record())
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / (candidate.name + ".log")).write_text(
            _candidate_log(candidate), encoding="utf-8")
        output_time += time.perf_counter() - write_started

    final_plan_path = run_dir / "final_multicore_res.json"
    if winner is not None and winner.plan is not None and fatal_error is None:
        write_started = time.perf_counter()
        _write_json(final_plan_path, winner.plan)
        _write_candidate_summary(run_dir / "summary.csv", candidates)
        output_time += time.perf_counter() - write_started

    candidate_generation_time = sum(
        candidate.generation_time for candidate in candidates)
    validation_time = sum(candidate.validation_time for candidate in candidates)
    cache_lookup_time = sum(
        candidate.cache_lookup_time for candidate in representatives)
    evaluation_time = sum(
        candidate.evaluation_time for candidate in representatives)
    total_wall_time = time.perf_counter() - total_started
    manifest = {
        "schema_version": 1,
        "problem": 1,
        "case": graph_path.stem,
        "cores": num_cores,
        "graph_path": str(graph_path),
        "config_path": str(config_path),
        "graph_hash": graph_hash,
        "config_hash": config_hash,
        "evaluator_hash": evaluator_hash,
        "evaluator_file_hashes": evaluator_file_hashes,
        "candidate_priority": list(CANDIDATE_PRIORITY),
        "generated_candidates": len(candidates),
        "unique_plans": len(representatives),
        "official_evaluations": official_evaluations,
        "cache_hits": cache_hits,
        "failed_candidates": [
            candidate.name for candidate in candidates
            if candidate.status == "failed"
        ],
        "winner": None if winner is None else {
            "name": winner.name,
            "makespan": winner.makespan,
            "added_copy_bytes": winner.added_copy_bytes,
            "canonical_signature": winner.canonical_signature,
        },
        "fatal_error": fatal_error,
        "timing": {
            "feature_time": feature_time,
            "candidate_generation_time": candidate_generation_time,
            "validation_time": validation_time,
            "evaluation_time": evaluation_time,
            "cache_lookup_time": cache_lookup_time,
            "output_time": output_time,
            "total_wall_time": total_wall_time,
        },
        "candidates": [
            candidate.manifest_record() for candidate in candidates
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = run_dir / "manifest.json"
    _write_json(manifest_path, manifest)

    if fatal_error is not None or winner is None or winner.plan is None:
        raise CandidateManagerError(
            fatal_error or "candidate manager did not produce a winner")
    return CandidateManagerResult(
        final_plan=winner.plan,
        winner_name=winner.name,
        winner_makespan=winner.makespan,
        winner_added_copy_bytes=winner.added_copy_bytes,
        manifest=manifest,
        manifest_path=manifest_path,
        final_plan_path=final_plan_path,
    )


__all__ = [
    "CANDIDATE_PRIORITY",
    "OFFICIAL_EVALUATOR_FILES",
    "CandidateManagerError",
    "CandidateManagerResult",
    "CandidatePlan",
    "EvaluationCache",
    "canonical_plan",
    "canonical_plan_signature",
    "deduplicate_candidates",
    "evaluate_official_problem1",
    "evaluation_key",
    "official_evaluator_hash",
    "run_candidate_manager",
    "select_best_candidate",
    "sha256_file",
]
