"""Candidate management for Problem-2 fixed-mapping ordering experiments."""

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
    validate_and_compact_result,
)
from candidate_manager_problem2_stage2 import (
    Stage1SelectedReference,
    load_verified_baseline_reference,
    load_verified_stage1_selected_references,
)
from contest_io import _read_json
from evaluation_validation import read_evaluation_config, validate_task_order
from multicore_cut_evaluate_problem_2 import read_scene_b_config
from solver_problem2_stage3 import (
    ORDER_POLICIES,
    generate_scene_b_order_candidates,
    subgraph_core_assignment,
)
from problem2_identity import plan_json_sha256
from stub_multicore_cut_and_schedule import derive_multicore_plan


SCHEMA_VERSION = 1
IMPLEMENTATION_VERSION = "problem2-stage3-fixed-mapping-ordering-gate-a-v1"
CANDIDATE_PRIORITY = ("fixed_mapping_original_order",) + tuple(
    "order_{}".format(policy) for policy in ORDER_POLICIES)


class Problem2Stage3Error(RuntimeError):
    """A Stage-3 experiment cannot be trusted or resumed safely."""


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


def _hash_named_files(
    files: Mapping[str, Path], version: str,
) -> Tuple[str, Dict[str, str]]:
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


def stage3_implementation_hash() -> Tuple[str, Dict[str, str]]:
    root = Path(__file__).resolve().parent
    return _hash_named_files({
        "candidate_manager_problem2_stage3.py": Path(__file__).resolve(),
        "solver_problem2_stage3.py": root / "solver_problem2_stage3.py",
        "candidate_manager_problem2.py": root / "candidate_manager_problem2.py",
        "candidate_manager_problem2_stage2.py": (
            root / "candidate_manager_problem2_stage2.py"),
        "problem2_identity.py": root / "problem2_identity.py",
    }, IMPLEMENTATION_VERSION)


@dataclass(frozen=True)
class FixedMappingReference:
    case: str
    cores: int
    family: str
    candidate: str
    plan: Dict[str, Any]
    plan_hash: str
    plan_file_hash: str
    canonical_signature: str
    metrics: Dict[str, Any]
    official_result: Dict[str, Any]
    official_result_json_hash: str
    stage2_group_path: Path
    stage2_group_file_hash: str
    stage2_run_identity_file_hash: str
    stage2_run_identity_hash: str

    def score(self) -> Tuple[int, int]:
        return (
            int(self.metrics["makespan_cycles"]),
            int(self.metrics["added_copy_bytes"]),
        )


@dataclass(frozen=True)
class Stage3GroupResult:
    manifest: Dict[str, Any]
    group_path: Path


def _candidate_by_name(group: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    matches = [record for record in group.get("candidates", [])
               if record.get("name") == name]
    if len(matches) != 1:
        raise Problem2Stage3Error(
            "Stage-2 group has {} records for {}".format(len(matches), name))
    return matches[0]


def _validate_run_identity(stage2_root: Path) -> Tuple[Dict[str, Any], Path, str]:
    path = Path(stage2_root) / "run_identity.json"
    if not path.is_file():
        raise Problem2Stage3Error("Stage-2 run_identity.json is missing")
    identity = _read_json(path)
    if not isinstance(identity, Mapping):
        raise Problem2Stage3Error("Stage-2 run identity is not an object")
    stored = identity.get("run_identity_sha256")
    payload = dict(identity)
    payload.pop("run_identity_sha256", None)
    if stored != json_sha256(payload):
        raise Problem2Stage3Error("Stage-2 run identity hash mismatch")
    if identity.get("mapping_policies") != ["locality"]:
        raise Problem2Stage3Error(
            "Stage-3 Gate A requires the frozen map_locality run")
    return dict(identity), path, sha256_file(path)


def load_verified_fixed_mapping_reference(
    *,
    graph_path: Path,
    config_path: Path,
    baseline_root: Path,
    stage2_root: Path,
    cores: int,
    family: str,
    candidate: str = "map_locality",
) -> FixedMappingReference:
    """Bind one frozen Stage-2 mapping plan without mutating its artifacts."""

    graph_path = Path(graph_path).resolve()
    config_path = Path(config_path).resolve()
    baseline_root = Path(baseline_root).resolve()
    stage2_root = Path(stage2_root).resolve()
    case = graph_path.stem
    run_identity, run_identity_path, run_identity_file_hash = (
        _validate_run_identity(stage2_root))
    evaluator_hash, _ = official_scene_b_hash()
    group_path = stage2_root / "groups" / case / "k{}".format(cores) / (
        family + ".json")
    if not group_path.is_file():
        raise Problem2Stage3Error(
            "Stage-2 fixed-mapping group is missing: {}".format(group_path))
    group = _read_json(group_path)
    expected = {
        "status": "success",
        "case": case,
        "cores": cores,
        "partition_family": family,
        "graph_sha256": sha256_file(graph_path),
        "config_sha256": sha256_file(config_path),
        "evaluator_sha256": evaluator_hash,
        "problem2_stage2_implementation_sha256": run_identity[
            "implementation_sha256"],
    }
    for name, value in expected.items():
        if group.get(name) != value:
            raise Problem2Stage3Error(
                "Stage-2 group identity mismatch for {}".format(name))
    record = _candidate_by_name(group, candidate)
    if (record.get("policy") != "locality" or not record.get("legal")
            or not isinstance(record.get("metrics"), Mapping)):
        raise Problem2Stage3Error("Stage-2 map_locality record is not reusable")

    group_dir = group_path.parent / family
    plan_path = group_dir / "plans" / (candidate + "_multicore_res.json")
    diagnostics_path = group_dir / "diagnostics" / (candidate + ".json")
    result_path = group_dir / "official_results" / (candidate + ".json")
    for path, field in (
        (plan_path, "plan_file_sha256"),
        (diagnostics_path, "diagnostics_file_sha256"),
        (result_path, "official_result_file_sha256"),
    ):
        if not path.is_file() or sha256_file(path) != record.get(field):
            raise Problem2Stage3Error(
                "Stage-2 {} artifact hash mismatch".format(path.name))

    plan = _read_json(plan_path)
    graph_json = _read_json(graph_path)
    derive = derive_multicore_plan(graph_json, plan)
    validate_task_order(derive)
    signature = canonical_plan_signature(plan)
    if signature != record.get("canonical_signature"):
        raise Problem2Stage3Error("Stage-2 fixed mapping signature mismatch")
    result = _read_json(result_path)
    if json_sha256(result) != record.get("official_result_json_sha256"):
        raise Problem2Stage3Error("Stage-2 fixed mapping result JSON mismatch")
    metrics = validate_and_compact_result(result)
    if metrics != record.get("metrics"):
        raise Problem2Stage3Error("Stage-2 fixed mapping metric mismatch")

    baseline = load_verified_baseline_reference(
        graph_path=graph_path,
        config_path=config_path,
        baseline_root=baseline_root,
        cores=cores,
        family=family,
    )
    serialized_partition = {
        int(node): int(subgraph)
        for node, subgraph in plan["node_to_subgraph"].items()
    }
    if serialized_partition != baseline.plan["node_to_subgraph"]:
        raise Problem2Stage3Error("Stage-2 mapping changed the fixed partition")
    if len(plan["core_schedules"]) != cores:
        raise Problem2Stage3Error("Stage-2 mapping has the wrong core count")
    assignment = subgraph_core_assignment(plan)
    if len(assignment) != len(derive["subgraph_ids"]):
        raise Problem2Stage3Error("Stage-2 mapping omits a subgraph")

    return FixedMappingReference(
        case=case,
        cores=cores,
        family=family,
        candidate=candidate,
        plan=copy.deepcopy(dict(plan)),
        plan_hash=plan_json_sha256(plan),
        plan_file_hash=sha256_file(plan_path),
        canonical_signature=signature,
        metrics=copy.deepcopy(metrics),
        official_result=copy.deepcopy(dict(result)),
        official_result_json_hash=json_sha256(result),
        stage2_group_path=group_path,
        stage2_group_file_hash=sha256_file(group_path),
        stage2_run_identity_file_hash=run_identity_file_hash,
        stage2_run_identity_hash=str(run_identity["run_identity_sha256"]),
    )


def _score(record: Mapping[str, Any]) -> Tuple[int, int, int]:
    metrics = record.get("metrics")
    if not isinstance(metrics, Mapping):
        raise Problem2Stage3Error("evaluated ordering lacks metrics")
    return (
        int(metrics["makespan_cycles"]),
        int(metrics["added_copy_bytes"]),
        CANDIDATE_PRIORITY.index(str(record["name"])),
    )


def _delta(
    candidate: Mapping[str, Any], reference: Mapping[str, Any],
) -> Dict[str, int]:
    return {
        field: int(candidate[field]) - int(reference[field])
        for field in (
            "makespan_cycles", "added_copy_bytes", "spill_added_copy_bytes")
    }


def _failed_record(name: str, stage: str, error: BaseException) -> Dict[str, Any]:
    return {
        "name": name,
        "family": "scene_b_fixed_mapping_ordering",
        "status": "failed",
        "legal": False,
        "failure_stage": stage,
        "error_type": type(error).__name__,
        "error": "{}: {}".format(type(error).__name__, error),
        "metrics": None,
    }


def run_stage3_ordering_group(
    *,
    graph_path: Path,
    config_path: Path,
    baseline_root: Path,
    stage2_root: Path,
    output_root: Path,
    cache_dir: Path,
    cores: int,
    family: str,
    policies: Sequence[str] = ORDER_POLICIES,
    evaluator: Callable[[Mapping[str, Any], Mapping[str, Any], Path], Dict[str, Any]] = evaluate_official_problem2,
) -> Stage3GroupResult:
    """Evaluate at most two unique orderings for one frozen mapping."""

    started = time.perf_counter()
    graph_path = Path(graph_path).resolve()
    config_path = Path(config_path).resolve()
    baseline_root = Path(baseline_root).resolve()
    stage2_root = Path(stage2_root).resolve()
    output_root = Path(output_root).resolve()
    cache_dir = Path(cache_dir).resolve()
    selected_policies = tuple(policies)
    if (not selected_policies or len(set(selected_policies)) != len(selected_policies)
            or set(selected_policies) - set(ORDER_POLICIES)
            or len(selected_policies) > 2):
        raise Problem2Stage3Error("invalid bounded ordering policy set")

    fixed = load_verified_fixed_mapping_reference(
        graph_path=graph_path,
        config_path=config_path,
        baseline_root=baseline_root,
        stage2_root=stage2_root,
        cores=cores,
        family=family,
    )
    selected = load_verified_stage1_selected_references(
        graph_root=graph_path.parent,
        config_path=config_path,
        baseline_root=baseline_root,
        pairs=((fixed.case, cores),),
    ).references[(fixed.case, cores)]
    graph_json = _read_json(graph_path)
    config = read_evaluation_config(str(config_path))
    scene_b = read_scene_b_config(str(config_path))
    evaluator_hash, evaluator_files = official_scene_b_hash()
    implementation_hash, implementation_files = stage3_implementation_hash()
    solver_path = Path(__file__).resolve().parent / "solver_problem2_stage3.py"
    solver_hash = sha256_file(solver_path)
    policy_hash = json_sha256({"policies": selected_policies})
    identity = {
        "graph_sha256": sha256_file(graph_path),
        "config_sha256": sha256_file(config_path),
        "evaluator_sha256": evaluator_hash,
        "solver_problem2_stage3_sha256": solver_hash,
        "problem2_stage3_implementation_sha256": implementation_hash,
        "fixed_mapping_plan_sha256": fixed.plan_hash,
        "fixed_mapping_plan_file_sha256": fixed.plan_file_hash,
        "fixed_mapping_result_json_sha256": fixed.official_result_json_hash,
        "stage2_group_file_sha256": fixed.stage2_group_file_hash,
        "stage2_run_identity_sha256": fixed.stage2_run_identity_hash,
        "stage2_run_identity_file_sha256": fixed.stage2_run_identity_file_hash,
        "stage1_selected_group_file_sha256": selected.group_file_hash,
        "stage1_selected_plan_file_sha256": selected.plan_file_hash,
        "stage1_selected_result_json_sha256": selected.official_result_json_hash,
        "ordering_policy_sha256": policy_hash,
        "ordering_policies": list(selected_policies),
    }
    run_dir = output_root / "groups" / fixed.case / "k{}".format(cores) / family
    plans_dir = run_dir / "plans"
    diagnostics_dir = run_dir / "diagnostics"
    records_dir = run_dir / "candidate_records"
    results_dir = run_dir / "official_results"

    original = {
        "name": "fixed_mapping_original_order",
        "family": "stage2_fixed_mapping_original_order",
        "policy": None,
        "status": "baseline_reference",
        "legal": True,
        "failure_stage": None,
        "error_type": None,
        "error": None,
        "deduplicated": False,
        "canonical_candidate": "fixed_mapping_original_order",
        "canonical_signature": fixed.canonical_signature,
        "plan_hash": fixed.plan_hash,
        "plan_file_sha256": fixed.plan_file_hash,
        "evaluation_key": None,
        "cache_hit": False,
        "official_result_json_sha256": fixed.official_result_json_hash,
        "metrics": copy.deepcopy(fixed.metrics),
        "timing": {
            "generation_seconds": 0.0,
            "validation_seconds": 0.0,
            "cache_lookup_seconds": 0.0,
            "official_evaluation_seconds": 0.0,
            "invocation_wall_seconds": 0.0,
        },
    }
    records: List[Dict[str, Any]] = [original]
    generation_started = time.perf_counter()
    try:
        generated = generate_scene_b_order_candidates(
            graph_json,
            fixed.plan,
            bandwidth=int(config["bandwidth"]),
            capacity=config["capacity"],
            cross_core_delay=int(scene_b["cross_core_copy_delay_cycles"]),
            policies=selected_policies,
        )
        generation_seconds = time.perf_counter() - generation_started
    except Exception as error:
        generation_seconds = time.perf_counter() - generation_started
        generated = {}
        records.extend(
            _failed_record("order_{}".format(policy), "generation", error)
            for policy in selected_policies
        )

    fixed_assignment = subgraph_core_assignment(fixed.plan)
    signature_owner: Dict[str, Dict[str, Any]] = {
        fixed.canonical_signature: original
    }
    cache = SceneBEvaluationCache(cache_dir)
    official_evaluations = 0
    cache_hits = 0
    for policy in selected_policies:
        name = "order_{}".format(policy)
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
            "family": "scene_b_fixed_mapping_ordering",
            "policy": policy,
            "status": "generated",
            "legal": False,
            "failure_stage": None,
            "error_type": None,
            "error": None,
            "plan_path": str(plan_path),
            "plan_file_sha256": sha256_file(plan_path),
            "plan_hash": plan_json_sha256(plan),
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
                "generation_seconds": generation_seconds / len(selected_policies),
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
            if plan["node_to_subgraph"] != fixed.plan["node_to_subgraph"]:
                raise Problem2Stage3Error("ordering changed the fixed partition")
            if subgraph_core_assignment(plan) != fixed_assignment:
                raise Problem2Stage3Error("ordering changed the fixed core mapping")
            signature = canonical_plan_signature(plan)
            record["canonical_signature"] = signature
            record["legal"] = True
            record["status"] = "valid"
        except Exception as error:
            failed = _failed_record(name, "validation", error)
            record.update({key: value for key, value in failed.items()
                           if key not in {"name", "family"}})
            signature = None
        record["timing"]["validation_seconds"] = (
            time.perf_counter() - validation_started)

        if signature is not None:
            if signature in signature_owner:
                owner = signature_owner[signature]
                if not isinstance(owner.get("metrics"), Mapping):
                    failed = _failed_record(
                        name,
                        "deduplication",
                        Problem2Stage3Error(
                            "equivalent representative did not produce "
                            "official metrics"),
                    )
                    record.update({
                        key_name: value for key_name, value in failed.items()
                        if key_name not in {"name", "family"}
                    })
                    record["canonical_candidate"] = owner["name"]
                else:
                    record["status"] = "deduplicated"
                    record["deduplicated"] = True
                    record["canonical_candidate"] = owner["name"]
                    record["metrics"] = copy.deepcopy(owner["metrics"])
                    record["official_result_path"] = owner.get(
                        "official_result_path")
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
                        if official_evaluations >= 2:
                            raise Problem2Stage3Error(
                                "per-group official evaluation budget exceeded")
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
                                "stage": "problem2_stage3_gate_a",
                                "case": fixed.case,
                                "cores": cores,
                                "partition_family": family,
                                "candidate": name,
                                "ordering_policy": policy,
                                "fixed_mapping_plan_sha256": fixed.plan_hash,
                                "ordering_policy_sha256": policy_hash,
                            },
                        )
                    else:
                        cache_hits += 1
                        official_seconds = float(
                            entry.get("official_evaluation_seconds", 0.0))
                        metrics = validate_and_compact_result(entry["result"])
                        if metrics != entry.get("metrics"):
                            raise Problem2Stage3Error("cache metric mismatch")
                        record["cache_hit"] = True
                    result = entry["result"]
                    result_path = results_dir / (name + ".json")
                    _write_json(result_path, result)
                    record["status"] = "evaluated"
                    record["metrics"] = metrics
                    record["official_result_path"] = str(result_path)
                    record["official_result_file_sha256"] = sha256_file(result_path)
                    record["official_result_json_sha256"] = json_sha256(result)
                    record["timing"]["official_evaluation_seconds"] = (
                        official_seconds)
                except Exception as error:
                    failed = _failed_record(name, "official_evaluation", error)
                    record.update({key_name: value for key_name, value in failed.items()
                                   if key_name not in {"name", "family"}})
        record["timing"]["invocation_wall_seconds"] = (
            time.perf_counter() - candidate_started)
        records.append(record)

    if official_evaluations > 2:
        raise Problem2Stage3Error("per-group official evaluation budget exceeded")
    candidate_record_hashes: Dict[str, str] = {}
    for record in records:
        path = records_dir / (str(record["name"]) + ".json")
        _write_json(path, record)
        candidate_record_hashes[str(record["name"])] = sha256_file(path)

    eligible = [record for record in records
                if record.get("legal") and isinstance(record.get("metrics"), Mapping)]
    if not eligible:
        raise Problem2Stage3Error("no legal ordering retained official metrics")
    fixed_winner = min(eligible, key=_score)
    fixed_score = fixed.score()
    stage1_score = selected.score()
    best_new = min(
        (record for record in eligible if str(record["name"]).startswith("order_")),
        key=_score,
        default=None,
    )
    new_beats_fixed = (
        best_new is not None and tuple(_score(best_new)[:2]) < fixed_score)
    new_beats_stage1 = (
        best_new is not None and tuple(_score(best_new)[:2]) < stage1_score)
    ordering_final_improvement = bool(new_beats_fixed and new_beats_stage1)

    if tuple(_score(fixed_winner)[:2]) < stage1_score:
        final_source = "stage3_fixed_mapping_portfolio"
        final_name = str(fixed_winner["name"])
        final_metrics = copy.deepcopy(fixed_winner["metrics"])
        if final_name == "fixed_mapping_original_order":
            final_plan = fixed.plan
            final_result = fixed.official_result
        else:
            final_plan = _read_json(Path(str(fixed_winner["plan_path"])))
            final_result = _read_json(Path(str(fixed_winner["official_result_path"])))
    else:
        final_source = "stage1_global_fallback"
        final_name = selected.winner
        final_metrics = copy.deepcopy(selected.metrics)
        final_plan = _read_json(selected.plan_path)
        final_result = _read_json(selected.official_result_path)

    winner_plan = fixed.plan if fixed_winner["name"] == (
        "fixed_mapping_original_order") else _read_json(
            Path(str(fixed_winner["plan_path"])))
    winner_plan_path = run_dir / "winner_fixed_mapping_multicore_res.json"
    final_plan_path = run_dir / "final_multicore_res.json"
    final_result_path = run_dir / "final_official_evaluation.json"
    _write_json(winner_plan_path, winner_plan)
    _write_json(final_plan_path, final_plan)
    _write_json(final_result_path, final_result)

    failed_ordering_candidates = sum(
        record.get("status") == "failed" for record in records[1:])
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "problem2_stage3_fixed_mapping_ordering_group",
        "implementation_version": IMPLEMENTATION_VERSION,
        "status": (
            "success" if failed_ordering_candidates == 0 else "partial_failure"),
        "case": fixed.case,
        "cores": cores,
        "partition_family": family,
        **identity,
        "evaluator_file_sha256": evaluator_files,
        "implementation_file_sha256": implementation_files,
        "candidate_priority": list(CANDIDATE_PRIORITY),
        "candidate_record_file_sha256": candidate_record_hashes,
        "generated_ordering_candidates": len(generated),
        "legal_ordering_candidates": sum(
            bool(record.get("legal")) for record in records[1:]),
        "failed_ordering_candidates": failed_ordering_candidates,
        "official_evaluations": official_evaluations,
        "cache_hits": cache_hits,
        "fixed_mapping_reference": {
            "candidate": fixed.candidate,
            "plan_hash": fixed.plan_hash,
            "plan_file_sha256": fixed.plan_file_hash,
            "canonical_signature": fixed.canonical_signature,
            "metrics": copy.deepcopy(fixed.metrics),
            "stage2_group_path": str(fixed.stage2_group_path),
            "stage2_group_file_sha256": fixed.stage2_group_file_hash,
        },
        "stage1_global_reference": {
            "winner": selected.winner,
            "score": list(stage1_score),
            "metrics": copy.deepcopy(selected.metrics),
            "group_file_sha256": selected.group_file_hash,
            "plan_file_sha256": selected.plan_file_hash,
            "official_result_json_sha256": selected.official_result_json_hash,
        },
        "best_new_ordering": None if best_new is None else {
            "name": best_new["name"],
            "score": list(_score(best_new)[:2]),
            "metrics": copy.deepcopy(best_new["metrics"]),
            "beats_fixed_mapping_original": new_beats_fixed,
            "beats_stage1_global_winner": new_beats_stage1,
        },
        "winner_within_fixed_mapping": {
            "name": fixed_winner["name"],
            "score": list(_score(fixed_winner)[:2]),
            "metrics": copy.deepcopy(fixed_winner["metrics"]),
            "plan_hash": plan_json_sha256(winner_plan),
        },
        "delta_vs_fixed_mapping_original_order": _delta(
            fixed_winner["metrics"], fixed.metrics),
        "final_case_core": {
            "source": final_source,
            "winner": final_name,
            "metrics": final_metrics,
            "delta_vs_stage1_global_winner": _delta(
                final_metrics, selected.metrics),
            "ordering_brought_final_case_core_improvement": (
                ordering_final_improvement),
        },
        "winner_plan_path": str(winner_plan_path),
        "winner_plan_file_sha256": sha256_file(winner_plan_path),
        "final_plan_path": str(final_plan_path),
        "final_plan_file_sha256": sha256_file(final_plan_path),
        "final_official_result_path": str(final_result_path),
        "final_official_result_file_sha256": sha256_file(final_result_path),
        "final_official_result_json_sha256": json_sha256(final_result),
        "candidates": records,
        "timing": {
            "generation_seconds": generation_seconds,
            "group_wall_seconds": time.perf_counter() - started,
        },
        "timestamp": _utc_now(),
    }
    group_path = output_root / "groups" / fixed.case / "k{}".format(cores) / (
        family + ".json")
    _write_json(group_path, manifest)
    return Stage3GroupResult(manifest=manifest, group_path=group_path)


def cache_key_summary(groups: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return a portable key-set manifest without copying the external cache."""

    sources: Dict[str, List[Dict[str, Any]]] = {}
    references = 0
    cache_hits = 0
    for group in groups:
        for record in group.get("candidates", [])[1:]:
            key = record.get("evaluation_key")
            if not key:
                continue
            references += 1
            cache_hits += int(bool(record.get("cache_hit")))
            sources.setdefault(str(key), []).append({
                "case": group.get("case"),
                "cores": group.get("cores"),
                "partition_family": group.get("partition_family"),
                "candidate": record.get("name"),
                "cache_hit": bool(record.get("cache_hit")),
                "plan_hash": record.get("plan_hash"),
                "official_result_json_sha256": record.get(
                    "official_result_json_sha256"),
            })
    keys = sorted(sources)
    return {
        "schema_version": 1,
        "kind": "problem2_cache_key_set_summary",
        "evaluation_key_references": references,
        "unique_evaluation_keys": len(keys),
        "cache_hit_references": cache_hits,
        "evaluation_key_set_sha256": json_sha256(keys),
        "entries": [
            {"evaluation_key": key, "sources": sources[key]} for key in keys
        ],
    }


def reusable_stage3_group(
    *,
    group_path: Path,
    graph_path: Path,
    config_path: Path,
    baseline_root: Path,
    stage2_root: Path,
    cores: int,
    family: str,
    policies: Sequence[str] = ORDER_POLICIES,
) -> Dict[str, Any] | None:
    """Return an intact matching Gate-A group, otherwise force safe rerun."""

    path = Path(group_path)
    if not path.is_file():
        return None
    try:
        group = _read_json(path)
        fixed = load_verified_fixed_mapping_reference(
            graph_path=graph_path,
            config_path=config_path,
            baseline_root=baseline_root,
            stage2_root=stage2_root,
            cores=cores,
            family=family,
        )
        implementation_hash, _ = stage3_implementation_hash()
        expected = {
            "status": "success",
            "case": Path(graph_path).stem,
            "cores": cores,
            "partition_family": family,
            "graph_sha256": sha256_file(graph_path),
            "config_sha256": sha256_file(config_path),
            "evaluator_sha256": official_scene_b_hash()[0],
            "problem2_stage3_implementation_sha256": implementation_hash,
            "fixed_mapping_plan_sha256": fixed.plan_hash,
            "ordering_policies": list(policies),
        }
        if any(group.get(name) != value for name, value in expected.items()):
            return None
        if int(group.get("failed_ordering_candidates", -1)) != 0:
            return None
        run_dir = path.parent / family
        for filename, hash_name in (
            ("winner_fixed_mapping_multicore_res.json", "winner_plan_file_sha256"),
            ("final_multicore_res.json", "final_plan_file_sha256"),
            ("final_official_evaluation.json",
             "final_official_result_file_sha256"),
        ):
            artifact = run_dir / filename
            if not artifact.is_file() or sha256_file(artifact) != group.get(hash_name):
                return None
        final_result_path = run_dir / "final_official_evaluation.json"
        if json_sha256(_read_json(final_result_path)) != group.get(
                "final_official_result_json_sha256"):
            return None
        record_hashes = group.get("candidate_record_file_sha256")
        if not isinstance(record_hashes, Mapping):
            return None
        for record in group.get("candidates", []):
            if record.get("status") == "failed":
                return None
            if record.get("legal") and not isinstance(record.get("metrics"), Mapping):
                return None
            name = str(record["name"])
            record_path = run_dir / "candidate_records" / (name + ".json")
            if (not record_path.is_file()
                    or sha256_file(record_path) != record_hashes.get(name)):
                return None
            if record.get("plan_path"):
                plan_path = run_dir / "plans" / (name + "_multicore_res.json")
                if (not plan_path.is_file()
                        or sha256_file(plan_path) != record.get("plan_file_sha256")):
                    return None
                plan = _read_json(plan_path)
                if plan_json_sha256(plan) != record.get("plan_hash"):
                    return None
                view = derive_multicore_plan(_read_json(graph_path), plan)
                validate_task_order(view)
                if canonical_plan_signature(plan) != record.get(
                        "canonical_signature"):
                    return None
                if subgraph_core_assignment(plan) != subgraph_core_assignment(
                        fixed.plan):
                    return None
                diagnostics_path = run_dir / "diagnostics" / (name + ".json")
                if (not diagnostics_path.is_file()
                        or sha256_file(diagnostics_path) != record.get(
                            "diagnostics_file_sha256")):
                    return None
            if record.get("status") == "evaluated":
                result_path = run_dir / "official_results" / (name + ".json")
                if (not result_path.is_file()
                        or sha256_file(result_path) != record.get(
                            "official_result_file_sha256")):
                    return None
                result = _read_json(result_path)
                if json_sha256(result) != record.get(
                        "official_result_json_sha256"):
                    return None
                if validate_and_compact_result(result) != record.get("metrics"):
                    return None
        return dict(group)
    except (OSError, ValueError, TypeError, KeyError, Problem2Stage3Error):
        return None

