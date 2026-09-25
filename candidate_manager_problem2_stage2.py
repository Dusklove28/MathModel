"""Candidate management for Problem-2 fixed-partition mapping experiments.

Stage-1 artifacts are treated as immutable references and are revalidated by
content.  Only newly mapped plans are sent to the unmodified official Scene-B
evaluator.  The baseline and every mapped attempt (including failures and
deduplications) are retained in a resumable, content-addressed record.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

from candidate_manager_problem1 import canonical_plan_signature, sha256_file
from candidate_manager_problem2 import (
    SceneBEvaluationCache,
    evaluate_official_problem2,
    evaluation_key,
    json_sha256,
    official_scene_b_hash,
    problem2_implementation_hash,
    validate_and_compact_result,
)
from contest_io import _read_json
from evaluation_validation import read_evaluation_config, validate_task_order
from multicore_cut_evaluate_problem_2 import read_scene_b_config
from solver_problem2 import MAPPING_POLICIES, generate_scene_b_mapping_candidates
from stub_multicore_cut_and_schedule import derive_multicore_plan


SCHEMA_VERSION = 1
IMPLEMENTATION_VERSION = "problem2-stage2-fixed-partition-mapping-v1"
MAPPING_FAMILIES = ("b0", "b1", "b2a_w4", "b2a_w8", "b2a_w16")
CANDIDATE_PRIORITY = ("original",) + tuple(
    "map_{}".format(policy) for policy in MAPPING_POLICIES)


class Problem2Stage2Error(RuntimeError):
    """A Stage-2 experiment cannot be trusted or resumed safely."""


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


def _hash_named_files(files: Mapping[str, Path], version: str) -> Tuple[str, Dict[str, str]]:
    hashes = {name: sha256_file(path) for name, path in files.items()}
    digest = hashlib.sha256()
    digest.update(version.encode("utf-8"))
    digest.update(b"\0")
    for name, value in sorted(hashes.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest(), hashes


def stage2_implementation_hash() -> Tuple[str, Dict[str, str]]:
    root = Path(__file__).resolve().parent
    return _hash_named_files({
        "candidate_manager_problem2_stage2.py": Path(__file__).resolve(),
        "candidate_manager_problem2.py": root / "candidate_manager_problem2.py",
        "solver_problem2.py": root / "solver_problem2.py",
    }, IMPLEMENTATION_VERSION)


@dataclass(frozen=True)
class BaselineReference:
    case: str
    cores: int
    family: str
    plan: Dict[str, Any]
    plan_hash: str
    canonical_signature: str
    metrics: Dict[str, Any]
    official_result_json_hash: str
    group_file_hash: str
    group_path: Path
    plan_path: Path
    official_result_path: Path
    identity: Dict[str, str]


@dataclass(frozen=True)
class Stage2GroupResult:
    manifest: Dict[str, Any]
    group_path: Path
    winner_plan_path: Path


def _candidate_by_name(group: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    matches = [candidate for candidate in group.get("candidates", [])
               if candidate.get("name") == name]
    if len(matches) != 1:
        raise Problem2Stage2Error(
            "baseline group has {} records for candidate {}".format(
                len(matches), name))
    return matches[0]


def load_verified_baseline_reference(
    *,
    graph_path: Path,
    config_path: Path,
    baseline_root: Path,
    cores: int,
    family: str,
) -> BaselineReference:
    """Load one Stage-1 candidate only after full local identity validation."""

    graph_path = Path(graph_path).resolve()
    config_path = Path(config_path).resolve()
    baseline_root = Path(baseline_root).resolve()
    if family not in MAPPING_FAMILIES:
        raise Problem2Stage2Error("unsupported mapping family: {}".format(family))
    case = graph_path.stem
    group_path = baseline_root / "groups" / case / "k{}.json".format(cores)
    if not group_path.is_file():
        raise Problem2Stage2Error("baseline group is missing: {}".format(group_path))
    group = _read_json(group_path)
    evaluator_hash, _ = official_scene_b_hash()
    stage1_impl_hash, _ = problem2_implementation_hash()
    expected = {
        "case": case,
        "cores": cores,
        "graph_sha256": sha256_file(graph_path),
        "config_sha256": sha256_file(config_path),
        "solver_sha256": sha256_file(Path(__file__).resolve().parent / "solver_problem1.py"),
        "evaluator_sha256": evaluator_hash,
        "problem2_implementation_sha256": stage1_impl_hash,
    }
    for name, value in expected.items():
        if group.get(name) != value:
            raise Problem2Stage2Error(
                "baseline identity mismatch for {}: expected {} got {}".format(
                    name, value, group.get(name)))
    if group.get("status") != "success":
        raise Problem2Stage2Error("baseline group status is not success")

    candidate = _candidate_by_name(group, family)
    if candidate.get("status") != "evaluated" or not candidate.get("legal"):
        raise Problem2Stage2Error("baseline candidate is not a legal evaluation")
    group_dir = baseline_root / "groups" / case / "k{}".format(cores)
    plan_path = group_dir / "plans" / (family + "_multicore_res.json")
    serialized_plan = _read_json(plan_path)
    try:
        if set(serialized_plan) != {"node_to_subgraph", "core_schedules"}:
            raise ValueError("unexpected plan fields")
        plan = {
            "node_to_subgraph": {
                int(node): int(subgraph)
                for node, subgraph in serialized_plan["node_to_subgraph"].items()
            },
            "core_schedules": copy.deepcopy(serialized_plan["core_schedules"]),
        }
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise Problem2Stage2Error(
            "baseline plan schema is invalid: {}".format(error)) from error
    plan_hash = json_sha256(plan)
    if plan_hash != candidate.get("plan_hash"):
        raise Problem2Stage2Error("baseline plan JSON hash mismatch")
    if sha256_file(plan_path) != candidate.get("plan_file_sha256"):
        raise Problem2Stage2Error("baseline plan file hash mismatch")
    graph_json = _read_json(graph_path)
    view = derive_multicore_plan(graph_json, plan)
    validate_task_order(view)
    signature = canonical_plan_signature(plan)
    if signature != candidate.get("canonical_signature"):
        raise Problem2Stage2Error("baseline canonical signature mismatch")

    canonical_name = str(candidate.get("canonical_candidate"))
    canonical = _candidate_by_name(group, canonical_name)
    result_path = group_dir / "official_results" / (canonical_name + ".json")
    result = _read_json(result_path)
    if sha256_file(result_path) != canonical.get("official_result_file_sha256"):
        raise Problem2Stage2Error("baseline official result file hash mismatch")
    result_json_hash = json_sha256(result)
    if result_json_hash != canonical.get("official_result_json_sha256"):
        raise Problem2Stage2Error("baseline official result JSON hash mismatch")
    metrics = validate_and_compact_result(result)
    if metrics != candidate.get("metrics"):
        raise Problem2Stage2Error("baseline official metrics mismatch")
    identity = {name: str(expected[name]) for name in (
        "graph_sha256", "config_sha256", "solver_sha256",
        "evaluator_sha256", "problem2_implementation_sha256",
    )}
    return BaselineReference(
        case=case,
        cores=cores,
        family=family,
        plan=plan,
        plan_hash=plan_hash,
        canonical_signature=signature,
        metrics=metrics,
        official_result_json_hash=result_json_hash,
        group_file_hash=sha256_file(group_path),
        group_path=group_path,
        plan_path=plan_path,
        official_result_path=result_path,
        identity=identity,
    )


def _failed_record(name: str, stage: str, error: BaseException) -> Dict[str, Any]:
    return {
        "name": name,
        "family": "scene_b_fixed_partition_mapping",
        "status": "failed",
        "legal": False,
        "failure_stage": stage,
        "error_type": type(error).__name__,
        "error": "{}: {}".format(type(error).__name__, error),
        "deduplicated": False,
        "canonical_candidate": None,
        "metrics": None,
        "timing": {},
    }


def _score(record: Mapping[str, Any]) -> Tuple[int, int, int]:
    metrics = record.get("metrics")
    if not isinstance(metrics, Mapping):
        raise Problem2Stage2Error("evaluated candidate lacks metrics")
    name = str(record["name"])
    return (
        int(metrics["makespan_cycles"]),
        int(metrics["added_copy_bytes"]),
        CANDIDATE_PRIORITY.index(name),
    )


def run_stage2_mapping_group(
    *,
    graph_path: Path,
    config_path: Path,
    baseline_root: Path,
    output_root: Path,
    cache_dir: Path,
    cores: int,
    family: str,
    max_moves: int = 2,
    search_width: int = 12,
    evaluator: Callable[[Mapping[str, Any], Mapping[str, Any], Path], Dict[str, Any]] = evaluate_official_problem2,
) -> Stage2GroupResult:
    """Run one strict pairing: original mapping versus three new mappings."""

    started = time.perf_counter()
    graph_path = Path(graph_path).resolve()
    config_path = Path(config_path).resolve()
    output_root = Path(output_root).resolve()
    cache_dir = Path(cache_dir).resolve()
    baseline = load_verified_baseline_reference(
        graph_path=graph_path,
        config_path=config_path,
        baseline_root=baseline_root,
        cores=cores,
        family=family,
    )
    graph_json = _read_json(graph_path)
    config = read_evaluation_config(str(config_path))
    scene_b = read_scene_b_config(str(config_path))
    evaluator_hash, evaluator_files = official_scene_b_hash()
    implementation_hash, implementation_files = stage2_implementation_hash()
    solver_path = Path(__file__).resolve().parent / "solver_problem2.py"
    solver_hash = sha256_file(solver_path)
    identity = {
        "graph_sha256": sha256_file(graph_path),
        "config_sha256": sha256_file(config_path),
        "evaluator_sha256": evaluator_hash,
        "solver_problem2_sha256": solver_hash,
        "problem2_stage2_implementation_sha256": implementation_hash,
        "baseline_group_file_sha256": baseline.group_file_hash,
        "baseline_plan_sha256": baseline.plan_hash,
        "baseline_result_json_sha256": baseline.official_result_json_hash,
        "search_sha256": json_sha256({
            "max_moves": max_moves,
            "search_width": search_width,
            "policies": MAPPING_POLICIES,
        }),
    }
    run_dir = output_root / "groups" / baseline.case / "k{}".format(cores) / family
    plans_dir = run_dir / "plans"
    records_dir = run_dir / "candidate_records"
    results_dir = run_dir / "official_results"
    diagnostics_dir = run_dir / "diagnostics"

    original = {
        "name": "original",
        "family": "stage1_fixed_partition_original_mapping",
        "status": "baseline_reference",
        "legal": True,
        "failure_stage": None,
        "error_type": None,
        "error": None,
        "deduplicated": False,
        "canonical_candidate": "original",
        "canonical_signature": baseline.canonical_signature,
        "plan_hash": baseline.plan_hash,
        "plan_path": str(baseline.plan_path),
        "official_result_path": str(baseline.official_result_path),
        "official_result_json_sha256": baseline.official_result_json_hash,
        "metrics": copy.deepcopy(baseline.metrics),
        "timing": {
            "generation_seconds": 0.0,
            "validation_seconds": 0.0,
            "cache_lookup_seconds": 0.0,
            "official_evaluation_seconds": 0.0,
            "invocation_wall_seconds": 0.0,
        },
    }
    records: List[Dict[str, Any]] = [original]
    generated: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    generation_started = time.perf_counter()
    try:
        generated = generate_scene_b_mapping_candidates(
            graph_json,
            baseline.plan,
            bandwidth=int(config["bandwidth"]),
            capacity=config["capacity"],
            cross_core_delay=int(scene_b["cross_core_copy_delay_cycles"]),
            max_moves=max_moves,
            search_width=search_width,
        )
        generation_seconds = time.perf_counter() - generation_started
    except Exception as error:
        generation_seconds = time.perf_counter() - generation_started
        for policy in MAPPING_POLICIES:
            records.append(_failed_record("map_{}".format(policy), "generation", error))

    signature_owner: Dict[str, Dict[str, Any]] = {
        baseline.canonical_signature: original
    }
    cache = SceneBEvaluationCache(cache_dir)
    official_evaluations = 0
    cache_hits = 0
    for policy in MAPPING_POLICIES:
        name = "map_{}".format(policy)
        if policy not in generated:
            continue
        plan, diagnostics = generated[policy]
        candidate_started = time.perf_counter()
        plan_path = plans_dir / (name + "_multicore_res.json")
        diagnostics_path = diagnostics_dir / (name + ".json")
        _write_json(plan_path, plan)
        _write_json(diagnostics_path, diagnostics)
        record: Dict[str, Any] = {
            "name": name,
            "family": "scene_b_fixed_partition_mapping",
            "policy": policy,
            "parameters": {
                "max_moves": max_moves,
                "search_width": search_width,
            },
            "status": "generated",
            "legal": False,
            "failure_stage": None,
            "error_type": None,
            "error": None,
            "plan_path": str(plan_path),
            "plan_file_sha256": sha256_file(plan_path),
            "plan_hash": json_sha256(plan),
            "diagnostics_path": str(diagnostics_path),
            "diagnostics_file_sha256": sha256_file(diagnostics_path),
            "canonical_signature": None,
            "canonical_candidate": None,
            "deduplicated": False,
            "cache_hit": False,
            "evaluation_key": None,
            "official_result_path": None,
            "official_result_file_sha256": None,
            "official_result_json_sha256": None,
            "metrics": None,
            "timing": {
                "generation_seconds": generation_seconds / len(MAPPING_POLICIES),
                "validation_seconds": 0.0,
                "cache_lookup_seconds": 0.0,
                "official_evaluation_seconds": 0.0,
                "invocation_wall_seconds": 0.0,
            },
        }
        validation_started = time.perf_counter()
        try:
            view = derive_multicore_plan(graph_json, plan)
            validate_task_order(view)
            if plan["node_to_subgraph"] != baseline.plan["node_to_subgraph"]:
                raise Problem2Stage2Error("mapped plan changed the fixed partition")
            signature = canonical_plan_signature(plan)
            record["canonical_signature"] = signature
            record["legal"] = True
            record["status"] = "valid"
        except Exception as error:
            record.update(_failed_record(name, "validation", error))
            record["plan_path"] = str(plan_path)
            record["plan_file_sha256"] = sha256_file(plan_path)
            record["plan_hash"] = json_sha256(plan)
            record["diagnostics_path"] = str(diagnostics_path)
            record["diagnostics_file_sha256"] = sha256_file(diagnostics_path)
            signature = None
        record["timing"]["validation_seconds"] = time.perf_counter() - validation_started
        if signature is not None:
            if signature in signature_owner:
                owner = signature_owner[signature]
                if not isinstance(owner.get("metrics"), Mapping):
                    failed = _failed_record(
                        name,
                        "deduplication",
                        Problem2Stage2Error(
                            "equivalent representative did not produce official metrics"),
                    )
                    for key_name, value in failed.items():
                        if key_name not in {"name", "family", "timing"}:
                            record[key_name] = value
                    record["canonical_candidate"] = owner["name"]
                else:
                    record["status"] = "deduplicated"
                    record["deduplicated"] = True
                    record["canonical_candidate"] = owner["name"]
                    record["metrics"] = copy.deepcopy(owner["metrics"])
                    record["official_result_path"] = owner.get("official_result_path")
                    record["official_result_json_sha256"] = owner.get(
                        "official_result_json_sha256")
            else:
                signature_owner[signature] = record
                record["canonical_candidate"] = name
                key = evaluation_key(
                    graph_hash=identity["graph_sha256"],
                    config_hash=identity["config_sha256"],
                    plan_hash=record["plan_hash"],
                    canonical_signature=signature,
                    evaluator_hash=evaluator_hash,
                    solver_hash=solver_hash,
                    implementation_hash=implementation_hash,
                )
                record["evaluation_key"] = key
                cache_identity = {
                    "evaluation_key": key,
                    "graph_sha256": identity["graph_sha256"],
                    "config_sha256": identity["config_sha256"],
                    "plan_sha256": record["plan_hash"],
                    "canonical_signature": signature,
                    "evaluator_sha256": evaluator_hash,
                    "solver_sha256": solver_hash,
                    "problem2_implementation_sha256": implementation_hash,
                }
                lookup_started = time.perf_counter()
                entry = cache.lookup(cache_identity)
                record["timing"]["cache_lookup_seconds"] = (
                    time.perf_counter() - lookup_started)
                try:
                    if entry is None:
                        official_evaluations += 1
                        official_started = time.perf_counter()
                        result = evaluator(graph_json, plan, config_path)
                        official_seconds = time.perf_counter() - official_started
                        metrics = validate_and_compact_result(result)
                        entry = cache.store(
                            cache_identity,
                            result,
                            metrics,
                            official_seconds,
                            metadata={
                                "case": baseline.case,
                                "cores": cores,
                                "partition_family": family,
                                "candidate": name,
                            },
                        )
                    else:
                        cache_hits += 1
                        official_seconds = float(
                            entry.get("official_evaluation_seconds", 0.0))
                        metrics = validate_and_compact_result(entry["result"])
                        if metrics != entry.get("metrics"):
                            raise Problem2Stage2Error("cache metric mismatch")
                        record["cache_hit"] = True
                    result = entry["result"]
                    result_path = results_dir / (name + ".json")
                    _write_json(result_path, result)
                    record["status"] = "evaluated"
                    record["metrics"] = metrics
                    record["official_result_path"] = str(result_path)
                    record["official_result_file_sha256"] = sha256_file(result_path)
                    record["official_result_json_sha256"] = json_sha256(result)
                    record["timing"]["official_evaluation_seconds"] = official_seconds
                except Exception as error:
                    failed = _failed_record(name, "official_evaluation", error)
                    for key_name, value in failed.items():
                        if key_name not in {"name", "family", "timing"}:
                            record[key_name] = value
        record["timing"]["invocation_wall_seconds"] = (
            time.perf_counter() - candidate_started)
        records.append(record)

    candidate_record_hashes: Dict[str, str] = {}
    for record in records:
        record_path = records_dir / (str(record["name"]) + ".json")
        _write_json(record_path, record)
        candidate_record_hashes[str(record["name"])] = sha256_file(record_path)
    eligible = [record for record in records
                if record.get("legal") and isinstance(record.get("metrics"), Mapping)]
    if not eligible:
        raise Problem2Stage2Error("no legal candidate retained official metrics")
    winner = min(eligible, key=_score)
    winner_plan = baseline.plan if winner["name"] == "original" else _read_json(
        Path(str(winner["plan_path"])))
    winner_plan_path = run_dir / "winner_multicore_res.json"
    _write_json(winner_plan_path, winner_plan)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "problem2_stage2_fixed_partition_mapping_group",
        "implementation_version": IMPLEMENTATION_VERSION,
        "status": "success",
        "case": baseline.case,
        "cores": cores,
        "partition_family": family,
        **identity,
        "evaluator_file_sha256": evaluator_files,
        "implementation_file_sha256": implementation_files,
        "candidate_priority": list(CANDIDATE_PRIORITY),
        "candidate_record_file_sha256": candidate_record_hashes,
        "generated_mapping_candidates": len(generated),
        "legal_mapping_candidates": sum(
            bool(record.get("legal")) for record in records[1:]),
        "failed_mapping_candidates": sum(
            record.get("status") == "failed" for record in records[1:]),
        "official_evaluations": official_evaluations,
        "cache_hits": cache_hits,
        "winner": {
            "name": winner["name"],
            "score": list(_score(winner)[:2]),
            "metrics": copy.deepcopy(winner["metrics"]),
            "plan_hash": json_sha256(winner_plan),
        },
        "baseline": {
            "group_path": str(baseline.group_path),
            "group_file_sha256": baseline.group_file_hash,
            "plan_path": str(baseline.plan_path),
            "plan_hash": baseline.plan_hash,
            "official_result_path": str(baseline.official_result_path),
            "official_result_json_sha256": baseline.official_result_json_hash,
            "metrics": copy.deepcopy(baseline.metrics),
        },
        "paired_delta": {
            "makespan_cycles": int(winner["metrics"]["makespan_cycles"])
            - int(baseline.metrics["makespan_cycles"]),
            "added_copy_bytes": int(winner["metrics"]["added_copy_bytes"])
            - int(baseline.metrics["added_copy_bytes"]),
            "spill_added_copy_bytes": int(winner["metrics"]["spill_added_copy_bytes"])
            - int(baseline.metrics["spill_added_copy_bytes"]),
        },
        "winner_plan_path": str(winner_plan_path),
        "winner_plan_file_sha256": sha256_file(winner_plan_path),
        "candidates": records,
        "timing": {
            "generation_seconds": generation_seconds,
            "group_wall_seconds": time.perf_counter() - started,
        },
        "timestamp": _utc_now(),
    }
    group_path = output_root / "groups" / baseline.case / "k{}".format(cores) / (
        family + ".json")
    _write_json(group_path, manifest)
    return Stage2GroupResult(
        manifest=manifest,
        group_path=group_path,
        winner_plan_path=winner_plan_path,
    )


def reusable_stage2_group(
    *,
    group_path: Path,
    graph_path: Path,
    config_path: Path,
    baseline_root: Path,
    cores: int,
    family: str,
    max_moves: int,
    search_width: int,
) -> Dict[str, Any] | None:
    """Return an intact matching group, otherwise ``None`` for safe rerun."""

    group_path = Path(group_path)
    if not group_path.is_file():
        return None
    try:
        group = _read_json(group_path)
        baseline = load_verified_baseline_reference(
            graph_path=graph_path,
            config_path=config_path,
            baseline_root=baseline_root,
            cores=cores,
            family=family,
        )
        evaluator_hash, _ = official_scene_b_hash()
        implementation_hash, _ = stage2_implementation_hash()
        expected = {
            "status": "success",
            "case": Path(graph_path).stem,
            "cores": cores,
            "partition_family": family,
            "graph_sha256": sha256_file(Path(graph_path)),
            "config_sha256": sha256_file(Path(config_path)),
            "evaluator_sha256": evaluator_hash,
            "solver_problem2_sha256": sha256_file(
                Path(__file__).resolve().parent / "solver_problem2.py"),
            "problem2_stage2_implementation_sha256": implementation_hash,
            "baseline_group_file_sha256": baseline.group_file_hash,
            "baseline_plan_sha256": baseline.plan_hash,
            "baseline_result_json_sha256": baseline.official_result_json_hash,
            "search_sha256": json_sha256({
                "max_moves": max_moves,
                "search_width": search_width,
                "policies": MAPPING_POLICIES,
            }),
        }
        if any(group.get(name) != value for name, value in expected.items()):
            return None
        winner_path = group_path.parent / family / "winner_multicore_res.json"
        if (not winner_path.is_file()
                or sha256_file(winner_path) != group.get("winner_plan_file_sha256")):
            return None
        for record in group.get("candidates", []):
            record_name = str(record.get("name"))
            record_path = group_path.parent / family / "candidate_records" / (
                record_name + ".json")
            expected_record_hash = group.get(
                "candidate_record_file_sha256", {}).get(record_name)
            if (not record_path.is_file()
                    or sha256_file(record_path) != expected_record_hash):
                return None
            if record_name == "original" or not record.get("legal"):
                continue
            plan_path = group_path.parent / family / "plans" / (
                record_name + "_multicore_res.json")
            if (not plan_path.is_file()
                    or sha256_file(plan_path) != record.get("plan_file_sha256")):
                return None
            diagnostics_path = group_path.parent / family / "diagnostics" / (
                record_name + ".json")
            if (not diagnostics_path.is_file()
                    or sha256_file(diagnostics_path)
                    != record.get("diagnostics_file_sha256")):
                return None
            if record.get("status") == "evaluated":
                result_path = group_path.parent / family / "official_results" / (
                    record_name + ".json")
                if (not result_path.is_file()
                        or sha256_file(result_path)
                        != record.get("official_result_file_sha256")):
                    return None
                result = _read_json(result_path)
                if json_sha256(result) != record.get(
                        "official_result_json_sha256"):
                    return None
        return group
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None

