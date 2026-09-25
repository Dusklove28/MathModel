"""Final candidate portfolio for Problem 2 without changing frozen solvers.

The portfolio combines the verified Stage-1 winner, the five frozen
``map_locality`` candidates, and the previous core count's final winner padded
with one empty core.  Every new inherited plan is checked and scored by the
unmodified official Scene-B evaluator.  The Stage-1 winner is always retained
as a safety fallback.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from candidate_manager_problem1 import canonical_plan_signature, sha256_file
from candidate_manager_problem2 import (
    SceneBEvaluationCache,
    evaluate_official_problem2,
    evaluation_key,
    json_sha256,
    official_scene_b_hash,
    pad_plan_with_empty_cores,
    validate_and_compact_result,
)
from candidate_manager_problem2_stage2 import (
    MAPPING_FAMILIES,
    Stage1SelectedReference,
    load_verified_stage1_selected_references,
    reusable_stage2_group,
    run_stage2_mapping_group,
    stage2_implementation_hash,
)
from contest_io import _read_json
from evaluation_validation import validate_task_order
from problem2_identity import plan_json_sha256
from stub_multicore_cut_and_schedule import derive_multicore_plan


SCHEMA_VERSION = 1
IMPLEMENTATION_VERSION = "problem2-final-portfolio-v1"
MAPPING_POLICY = "locality"


class Problem2FinalError(RuntimeError):
    """A final portfolio group cannot be trusted or reproduced."""


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


def _aggregate_hash(named_hashes: Mapping[str, str], version: str) -> str:
    digest = hashlib.sha256()
    digest.update(version.encode("utf-8"))
    digest.update(b"\0")
    for name, value in sorted(named_hashes.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def final_implementation_hash(
    runner_path: Path | None = None,
) -> Tuple[str, Dict[str, str]]:
    root = Path(__file__).resolve().parent
    files = {
        "candidate_manager_problem2_final.py": Path(__file__).resolve(),
        "candidate_manager_problem2.py": root / "candidate_manager_problem2.py",
        "candidate_manager_problem2_stage2.py": (
            root / "candidate_manager_problem2_stage2.py"),
        "problem2_identity.py": root / "problem2_identity.py",
        "solver_problem1.py": root / "solver_problem1.py",
        "solver_problem2.py": root / "solver_problem2.py",
        "problem2_preregistered_split.json": (
            root / "problem2_preregistered_split.json"),
    }
    if runner_path is not None:
        files[Path(runner_path).name] = Path(runner_path).resolve()
    hashes = {name: sha256_file(path) for name, path in files.items()}
    return _aggregate_hash(hashes, IMPLEMENTATION_VERSION), hashes


@dataclass
class FinalCandidate:
    name: str
    family: str
    priority: int
    status: str = "pending"
    legal: bool = False
    plan: Dict[str, Any] | None = None
    result: Dict[str, Any] | None = None
    metrics: Dict[str, Any] | None = None
    plan_hash: str | None = None
    plan_path: str | None = None
    plan_file_sha256: str | None = None
    canonical_signature: str | None = None
    official_result_json_sha256: str | None = None
    official_result_path: str | None = None
    official_result_file_sha256: str | None = None
    evaluation_key: str | None = None
    cache_hit: bool = False
    deduplicated: bool = False
    canonical_candidate: str | None = None
    failure_stage: str | None = None
    error_type: str | None = None
    error: str | None = None
    source: Dict[str, Any] = field(default_factory=dict)
    timing: Dict[str, float] = field(default_factory=dict)

    def score(self) -> Tuple[int, int, int]:
        if not self.legal or self.metrics is None:
            raise Problem2FinalError("candidate is not eligible: {}".format(self.name))
        return (
            int(self.metrics["makespan_cycles"]),
            int(self.metrics["added_copy_bytes"]),
            self.priority,
        )

    def record(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family,
            "priority": self.priority,
            "status": self.status,
            "legal": self.legal,
            "failure_stage": self.failure_stage,
            "error_type": self.error_type,
            "error": self.error,
            "plan_hash": self.plan_hash,
            "plan_path": self.plan_path,
            "plan_file_sha256": self.plan_file_sha256,
            "canonical_signature": self.canonical_signature,
            "official_result_json_sha256": self.official_result_json_sha256,
            "official_result_path": self.official_result_path,
            "official_result_file_sha256": self.official_result_file_sha256,
            "evaluation_key": self.evaluation_key,
            "cache_hit": self.cache_hit,
            "deduplicated": self.deduplicated,
            "canonical_candidate": self.canonical_candidate,
            "source": copy.deepcopy(self.source),
            "metrics": copy.deepcopy(self.metrics),
            "timing": copy.deepcopy(self.timing),
        }


def _fail(candidate: FinalCandidate, stage: str, error: BaseException) -> None:
    candidate.status = "failed"
    candidate.legal = False
    candidate.failure_stage = stage
    candidate.error_type = type(error).__name__
    candidate.error = "{}: {}".format(type(error).__name__, error)


def _validate_plan(
    graph_json: Mapping[str, Any], candidate: FinalCandidate,
) -> None:
    if candidate.plan is None:
        raise Problem2FinalError("candidate has no plan")
    view = derive_multicore_plan(graph_json, candidate.plan)
    validate_task_order(view)
    candidate.plan_hash = plan_json_sha256(candidate.plan)
    candidate.canonical_signature = canonical_plan_signature(candidate.plan)
    candidate.legal = True


def stage1_candidate(
    reference: Stage1SelectedReference,
    graph_json: Mapping[str, Any],
    priority: int = 0,
) -> FinalCandidate:
    candidate = FinalCandidate(
        name="stage1_actual_winner",
        family="verified_stage1_global_fallback",
        priority=priority,
        source={
            "winner": reference.winner,
            "group_file_sha256": reference.group_file_hash,
            "plan_file_sha256": reference.plan_file_hash,
            "official_result_json_sha256": reference.official_result_json_hash,
        },
    )
    try:
        candidate.plan = _read_json(reference.plan_path)
        candidate.result = _read_json(reference.official_result_path)
        _validate_plan(graph_json, candidate)
        candidate.plan_file_sha256 = sha256_file(reference.plan_path)
        candidate.plan_path = str(reference.plan_path)
        candidate.metrics = validate_and_compact_result(candidate.result)
        if json_sha256(candidate.metrics) != json_sha256(reference.metrics):
            raise Problem2FinalError("Stage-1 compact metrics changed")
        candidate.official_result_json_sha256 = json_sha256(candidate.result)
        candidate.official_result_path = str(reference.official_result_path)
        candidate.official_result_file_sha256 = sha256_file(
            reference.official_result_path)
        if (candidate.official_result_json_sha256
                != reference.official_result_json_hash):
            raise Problem2FinalError("Stage-1 official JSON hash changed")
        candidate.status = "verified_reference"
        candidate.canonical_candidate = candidate.name
    except Exception as error:
        _fail(candidate, "stage1_reference_validation", error)
    return candidate


def stage1_named_candidate(
    *, baseline_root: Path, case: str, cores: int, name: str,
    graph_json: Mapping[str, Any], priority: int,
) -> FinalCandidate:
    """Load one literal Stage-1 candidate from verified local artifacts."""

    candidate = FinalCandidate(
        name=name,
        family="stage1_literal_candidate",
        priority=priority,
        source={"stage1_candidate": name},
    )
    try:
        group_path = (
            Path(baseline_root) / "groups" / case / "k{}.json".format(cores))
        group = _read_json(group_path)
        matches = [record for record in group.get("candidates", [])
                   if record.get("name") == name]
        if len(matches) != 1:
            raise Problem2FinalError(
                "Stage-1 group has {} {} records".format(len(matches), name))
        record = matches[0]
        if not record.get("legal") or record.get("status") not in (
                "evaluated", "deduplicated"):
            raise Problem2FinalError(
                "Stage-1 {} is not a verified legal evaluation".format(name))
        group_dir = Path(baseline_root) / "groups" / case / "k{}".format(cores)
        plan_path = group_dir / "plans" / (name + "_multicore_res.json")
        result_name = (str(record.get("canonical_candidate"))
                       if record.get("status") == "deduplicated" else name)
        result_path = group_dir / "official_results" / (result_name + ".json")
        if (not plan_path.is_file()
                or sha256_file(plan_path) != record.get("plan_file_sha256")):
            raise Problem2FinalError("Stage-1 candidate plan hash mismatch")
        candidate.plan = _read_json(plan_path)
        candidate.result = _read_json(result_path)
        _validate_plan(graph_json, candidate)
        if candidate.canonical_signature != record.get("canonical_signature"):
            raise Problem2FinalError("Stage-1 candidate signature mismatch")
        candidate.source["historical_plan_hash"] = record.get("plan_hash")
        candidate.plan_path = str(plan_path)
        candidate.plan_file_sha256 = sha256_file(plan_path)
        candidate.metrics = validate_and_compact_result(candidate.result)
        if json_sha256(candidate.metrics) != json_sha256(record.get("metrics")):
            raise Problem2FinalError("Stage-1 candidate compact metrics mismatch")
        candidate.official_result_json_sha256 = json_sha256(candidate.result)
        if (candidate.official_result_json_sha256
                != record.get("official_result_json_sha256")):
            raise Problem2FinalError("Stage-1 candidate official JSON mismatch")
        candidate.official_result_path = str(result_path)
        candidate.official_result_file_sha256 = sha256_file(result_path)
        expected_result_file_hash = record.get("official_result_file_sha256")
        if (expected_result_file_hash is not None
                and candidate.official_result_file_sha256
                != expected_result_file_hash):
            raise Problem2FinalError("Stage-1 candidate result file mismatch")
        candidate.evaluation_key = record.get("evaluation_key")
        candidate.cache_hit = bool(record.get("cache_hit", False))
        candidate.timing = copy.deepcopy(record.get("timing") or {})
        candidate.source["stage1_group_file_sha256"] = sha256_file(group_path)
        candidate.status = "verified_stage1_literal"
        candidate.canonical_candidate = candidate.name
    except Exception as error:
        _fail(candidate, "stage1_literal_validation", error)
    return candidate


def _stage2_reference_candidate(
    *,
    group: Mapping[str, Any],
    group_dir: Path,
    family: str,
    stage2_name: str,
    final_name: str,
    final_family: str,
    graph_json: Mapping[str, Any],
    priority: int,
) -> FinalCandidate:
    candidate = FinalCandidate(
        name=final_name,
        family=final_family,
        priority=priority,
        source={
            "partition_family": family,
            "stage2_candidate": stage2_name,
            "stage2_group_file_sha256": sha256_file(
                Path(group_dir).with_suffix(".json")),
        },
    )
    try:
        matches = [item for item in group.get("candidates", [])
                   if item.get("name") == stage2_name]
        if len(matches) != 1:
            raise Problem2FinalError(
                "mapping group has {} {} records".format(
                    len(matches), stage2_name))
        record = matches[0]
        allowed_statuses = (
            ("baseline_reference",) if stage2_name == "original"
            else ("evaluated", "deduplicated"))
        if (record.get("status") not in allowed_statuses
                or not record.get("legal")):
            raise Problem2FinalError(
                "{} is not a verified legal Stage-2 reference".format(
                    stage2_name))
        if stage2_name == "original":
            plan_path = Path(str(record.get("plan_path", "")))
        else:
            plan_path = Path(group_dir) / "plans" / (
                stage2_name + "_multicore_res.json")
        if record.get("status") == "evaluated":
            result_path = (
                Path(group_dir) / "official_results" / (stage2_name + ".json"))
        else:
            result_path = Path(str(record.get("official_result_path", "")))
            if not result_path.is_file():
                result_path = Path(str(
                    group.get("baseline", {}).get("official_result_path", "")))
        expected_plan_file_hash = record.get("plan_file_sha256")
        if (expected_plan_file_hash is not None
                and sha256_file(plan_path) != expected_plan_file_hash):
            raise Problem2FinalError("mapping plan file hash mismatch")
        expected_result_file_hash = record.get("official_result_file_sha256")
        if (expected_result_file_hash is not None
                and sha256_file(result_path) != expected_result_file_hash):
            raise Problem2FinalError("mapping result file hash mismatch")
        candidate.plan = _read_json(plan_path)
        candidate.result = _read_json(result_path)
        _validate_plan(graph_json, candidate)
        if candidate.plan_hash != record.get("plan_hash"):
            raise Problem2FinalError("mapping canonical plan hash mismatch")
        candidate.plan_file_sha256 = sha256_file(plan_path)
        candidate.plan_path = str(plan_path)
        candidate.metrics = validate_and_compact_result(candidate.result)
        if json_sha256(candidate.metrics) != json_sha256(record.get("metrics")):
            raise Problem2FinalError("mapping compact metrics mismatch")
        candidate.official_result_json_sha256 = json_sha256(candidate.result)
        candidate.official_result_path = str(result_path)
        candidate.official_result_file_sha256 = sha256_file(result_path)
        if (candidate.official_result_json_sha256
                != record.get("official_result_json_sha256")):
            raise Problem2FinalError("mapping official JSON hash mismatch")
        candidate.evaluation_key = record.get("evaluation_key")
        candidate.cache_hit = bool(record.get("cache_hit", False))
        candidate.status = "verified_stage2_{}".format(record.get("status"))
        candidate.canonical_candidate = candidate.name
        candidate.timing = copy.deepcopy(record.get("timing") or {})
    except Exception as error:
        _fail(candidate, "mapping_reference_validation", error)
    return candidate


def original_mapping_candidate(
    *, group: Mapping[str, Any], group_dir: Path, family: str,
    graph_json: Mapping[str, Any], priority: int,
) -> FinalCandidate:
    return _stage2_reference_candidate(
        group=group,
        group_dir=group_dir,
        family=family,
        stage2_name="original",
        final_name="original@{}".format(family),
        final_family="stage1_fixed_partition_original_mapping",
        graph_json=graph_json,
        priority=priority,
    )


def mapping_candidate(
    *, group: Mapping[str, Any], group_dir: Path, family: str,
    graph_json: Mapping[str, Any], priority: int,
) -> FinalCandidate:
    return _stage2_reference_candidate(
        group=group,
        group_dir=group_dir,
        family=family,
        stage2_name="map_locality",
        final_name="map_locality@{}".format(family),
        final_family="scene_b_fixed_partition_mapping",
        graph_json=graph_json,
        priority=priority,
    )


def inherited_candidate(
    *,
    graph_json: Mapping[str, Any],
    graph_hash: str,
    config_path: Path,
    config_hash: str,
    source_plan: Mapping[str, Any],
    source_cores: int,
    target_cores: int,
    source_group_hash: str,
    priority: int,
    cache_dir: Path,
    implementation_hash: str,
    solver_hash: str,
    existing_by_signature: Mapping[str, FinalCandidate],
) -> FinalCandidate:
    candidate = FinalCandidate(
        name="inherit_final_k{}".format(source_cores),
        family="recursive_final_winner_inheritance",
        priority=priority,
        source={
            "source_cores": source_cores,
            "target_cores": target_cores,
            "source_group_file_sha256": source_group_hash,
            "padding": "append_empty_core_schedules",
        },
    )
    started = time.perf_counter()
    try:
        generation_started = time.perf_counter()
        candidate.plan = pad_plan_with_empty_cores(source_plan, target_cores)
        candidate.timing["generation_seconds"] = (
            time.perf_counter() - generation_started)
        validation_started = time.perf_counter()
        _validate_plan(graph_json, candidate)
        candidate.timing["validation_seconds"] = (
            time.perf_counter() - validation_started)
        assert candidate.canonical_signature is not None
        duplicate = existing_by_signature.get(candidate.canonical_signature)
        if duplicate is not None:
            if duplicate.metrics is None or duplicate.result is None:
                raise Problem2FinalError("duplicate owner lacks official result")
            candidate.deduplicated = True
            candidate.canonical_candidate = duplicate.name
            candidate.metrics = copy.deepcopy(duplicate.metrics)
            candidate.result = copy.deepcopy(duplicate.result)
            candidate.official_result_json_sha256 = (
                duplicate.official_result_json_sha256)
            candidate.official_result_path = duplicate.official_result_path
            candidate.official_result_file_sha256 = (
                duplicate.official_result_file_sha256)
            candidate.evaluation_key = duplicate.evaluation_key
            candidate.status = "deduplicated"
            candidate.legal = True
            candidate.timing["official_evaluation_seconds"] = 0.0
            candidate.timing["cache_lookup_seconds"] = 0.0
            return candidate

        evaluator_hash, _ = official_scene_b_hash()
        assert candidate.plan_hash is not None
        key = evaluation_key(
            graph_hash=graph_hash,
            config_hash=config_hash,
            plan_hash=candidate.plan_hash,
            canonical_signature=candidate.canonical_signature,
            evaluator_hash=evaluator_hash,
            solver_hash=solver_hash,
            implementation_hash=implementation_hash,
        )
        candidate.evaluation_key = key
        identity = {
            "evaluation_key": key,
            "graph_sha256": graph_hash,
            "config_sha256": config_hash,
            "plan_sha256": candidate.plan_hash,
            "canonical_signature": candidate.canonical_signature,
            "evaluator_sha256": evaluator_hash,
            "solver_sha256": solver_hash,
            "problem2_implementation_sha256": implementation_hash,
        }
        cache = SceneBEvaluationCache(cache_dir)
        lookup_started = time.perf_counter()
        entry = cache.lookup(identity)
        candidate.timing["cache_lookup_seconds"] = (
            time.perf_counter() - lookup_started)
        if entry is None:
            official_started = time.perf_counter()
            candidate.result = evaluate_official_problem2(
                graph_json, candidate.plan, config_path)
            official_seconds = time.perf_counter() - official_started
            candidate.metrics = validate_and_compact_result(candidate.result)
            entry = cache.store(
                identity,
                candidate.result,
                candidate.metrics,
                official_seconds,
                metadata={
                    "candidate": candidate.name,
                    "source_cores": source_cores,
                    "target_cores": target_cores,
                    "kind": "problem2_final_recursive_inheritance",
                },
            )
        else:
            candidate.cache_hit = True
            candidate.result = copy.deepcopy(entry["result"])
            candidate.metrics = validate_and_compact_result(candidate.result)
            if json_sha256(candidate.metrics) != json_sha256(
                    entry.get("metrics")):
                raise Problem2FinalError("inherited cache metric mismatch")
            candidate.source["cached_original_official_evaluation_seconds"] = (
                float(entry.get("official_evaluation_seconds", 0.0)))
            official_seconds = 0.0
        candidate.timing["official_evaluation_seconds"] = official_seconds
        candidate.official_result_json_sha256 = json_sha256(candidate.result)
        candidate.official_result_path = str(entry["resolved_result_path"])
        candidate.official_result_file_sha256 = str(entry["result_file_sha256"])
        candidate.canonical_candidate = candidate.name
        candidate.status = "evaluated"
    except Exception as error:
        _fail(candidate, "inheritance_evaluation", error)
    finally:
        candidate.timing["invocation_wall_seconds"] = (
            time.perf_counter() - started)
    return candidate


def _deduplicate_candidates(candidates: Sequence[FinalCandidate]) -> None:
    owners: Dict[str, FinalCandidate] = {}
    for candidate in sorted(candidates, key=lambda item: item.priority):
        if not candidate.legal or candidate.canonical_signature is None:
            continue
        owner = owners.get(candidate.canonical_signature)
        if owner is None:
            owners[candidate.canonical_signature] = candidate
            candidate.canonical_candidate = candidate.name
            continue
        if json_sha256(candidate.metrics) != json_sha256(owner.metrics):
            raise Problem2FinalError(
                "equivalent plans have different official metrics: {} and {}"
                .format(owner.name, candidate.name))
        candidate.deduplicated = True
        candidate.canonical_candidate = owner.name


def reusable_final_group(
    *, group_path: Path, run_identity_sha256: str,
    case: str, cores: int,
) -> Dict[str, Any] | None:
    group_path = Path(group_path)
    if not group_path.is_file():
        return None
    try:
        group = _read_json(group_path)
        expected = {
            "schema_version": SCHEMA_VERSION,
            "kind": "problem2_final_portfolio_group",
            "status": "success",
            "case": case,
            "cores": cores,
            "run_identity_sha256": run_identity_sha256,
        }
        for key, value in expected.items():
            if group.get(key) != value:
                return None
        group_dir = group_path.parent / "k{}".format(cores)
        plan_path = group_dir / "final_multicore_res.json"
        result_path = group_dir / "final_official_evaluation.json"
        if (not plan_path.is_file() or not result_path.is_file()
                or sha256_file(plan_path) != group.get("final_plan_file_sha256")
                or sha256_file(result_path)
                != group.get("final_official_result_file_sha256")):
            return None
        plan = _read_json(plan_path)
        result = _read_json(result_path)
        if plan_json_sha256(plan) != group.get("final_plan_hash"):
            return None
        if json_sha256(result) != group.get("final_official_result_json_sha256"):
            return None
        metrics = validate_and_compact_result(result)
        if metrics != group.get("winner", {}).get("metrics"):
            return None
        output_root = group_path.parents[2]
        for family, expected_hash in (
                group.get("mapping_group_file_sha256") or {}).items():
            mapping_group_path = (
                output_root / "mapping" / "groups" / case
                / "k{}".format(cores) / (str(family) + ".json"))
            if (not mapping_group_path.is_file()
                    or sha256_file(mapping_group_path) != expected_hash):
                return None
        for record in group.get("candidates", []):
            if (record.get("family")
                    != "recursive_final_winner_inheritance"
                    or not record.get("legal")):
                continue
            inherited_plan_path = group_dir / "candidate_plans" / (
                str(record.get("name")) + "_multicore_res.json")
            if (not inherited_plan_path.is_file()
                    or sha256_file(inherited_plan_path)
                    != record.get("plan_file_sha256")):
                return None
            inherited_plan = _read_json(inherited_plan_path)
            if plan_json_sha256(inherited_plan) != record.get("plan_hash"):
                return None
        return group
    except Exception:
        return None


def run_final_case(
    *,
    graph_path: Path,
    config_path: Path,
    baseline_root: Path,
    output_root: Path,
    cache_dir: Path,
    max_cores: int,
    max_moves: int,
    search_width: int,
    run_identity_sha256: str,
    implementation_hash: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run one case sequentially so the final winner can inherit upward."""

    graph_path = Path(graph_path).resolve()
    config_path = Path(config_path).resolve()
    baseline_root = Path(baseline_root).resolve()
    output_root = Path(output_root).resolve()
    cache_dir = Path(cache_dir).resolve()
    case = graph_path.stem
    references = load_verified_stage1_selected_references(
        graph_root=graph_path.parent,
        config_path=config_path,
        baseline_root=baseline_root,
        pairs=[(case, cores) for cores in range(1, max_cores + 1)],
    ).references
    graph_json = _read_json(graph_path)
    graph_hash = sha256_file(graph_path)
    config_hash = sha256_file(config_path)
    stage2_root = output_root / "mapping"
    stage2_cache = cache_dir / "mapping"
    inherited_cache = cache_dir / "inheritance"
    solver_hash = _aggregate_hash({
        "candidate_manager_problem2.py": sha256_file(
            Path(__file__).resolve().parent / "candidate_manager_problem2.py"),
        "solver_problem2.py": sha256_file(
            Path(__file__).resolve().parent / "solver_problem2.py"),
    }, "problem2-final-inheritance-plan-builder-v1")
    groups: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    previous_plan: Dict[str, Any] | None = None
    previous_group_hash: str | None = None

    for cores in range(1, max_cores + 1):
        group_path = output_root / "groups" / case / "k{}.json".format(cores)
        reusable = reusable_final_group(
            group_path=group_path,
            run_identity_sha256=run_identity_sha256,
            case=case,
            cores=cores,
        )
        if reusable is not None:
            found = dict(reusable)
            found["_resume"] = True
            groups.append(found)
            previous_plan = _read_json(
                output_root / "groups" / case / "k{}".format(cores)
                / "final_multicore_res.json")
            previous_group_hash = sha256_file(group_path)
            continue

        started = time.perf_counter()
        stage1 = stage1_candidate(references[(case, cores)], graph_json, 0)
        if not stage1.legal or stage1.metrics is None or stage1.result is None:
            raise Problem2FinalError(
                "Stage-1 safety fallback failed for {} k{}: {}".format(
                    case, cores, stage1.error))
        single = stage1_named_candidate(
            baseline_root=baseline_root,
            case=case,
            cores=cores,
            name="single",
            graph_json=graph_json,
            priority=1,
        )
        if not single.legal:
            raise Problem2FinalError(
                "literal single safety candidate failed for {} k{}: {}".format(
                    case, cores, single.error))
        candidates: List[FinalCandidate] = [stage1, single]
        mapping_group_hashes: Dict[str, str] = {}
        family_failures: List[Dict[str, Any]] = []

        if cores >= 2:
            for family_index, family in enumerate(MAPPING_FAMILIES, start=1):
                stage2_group_path = (
                    stage2_root / "groups" / case / "k{}".format(cores)
                    / (family + ".json"))
                try:
                    stage2_group = reusable_stage2_group(
                        group_path=stage2_group_path,
                        graph_path=graph_path,
                        config_path=config_path,
                        baseline_root=baseline_root,
                        cores=cores,
                        family=family,
                        max_moves=max_moves,
                        search_width=search_width,
                        mapping_policies=(MAPPING_POLICY,),
                    )
                    if stage2_group is None:
                        stage2_group = run_stage2_mapping_group(
                            graph_path=graph_path,
                            config_path=config_path,
                            baseline_root=baseline_root,
                            output_root=stage2_root,
                            cache_dir=stage2_cache,
                            cores=cores,
                            family=family,
                            max_moves=max_moves,
                            search_width=search_width,
                            mapping_policies=(MAPPING_POLICY,),
                        ).manifest
                    mapping_group_hashes[family] = sha256_file(stage2_group_path)
                    original = original_mapping_candidate(
                        group=stage2_group,
                        group_dir=stage2_group_path.with_suffix(""),
                        family=family,
                        graph_json=graph_json,
                        priority=1 + family_index,
                    )
                    candidate = mapping_candidate(
                        group=stage2_group,
                        group_dir=stage2_group_path.with_suffix(""),
                        family=family,
                        graph_json=graph_json,
                        priority=1 + len(MAPPING_FAMILIES) + family_index,
                    )
                    candidates.extend((original, candidate))
                    for checked in (original, candidate):
                        if checked.legal:
                            continue
                        family_failures.append({
                            "case": case, "cores": cores,
                            "candidate": checked.name,
                            "error_type": checked.error_type,
                            "error": checked.error,
                        })
                except Exception as error:
                    for name, candidate_family, priority in (
                        ("original@{}".format(family),
                         "stage1_fixed_partition_original_mapping",
                         1 + family_index),
                        ("map_locality@{}".format(family),
                         "scene_b_fixed_partition_mapping",
                         1 + len(MAPPING_FAMILIES) + family_index),
                    ):
                        failed = FinalCandidate(
                            name=name,
                            family=candidate_family,
                            priority=priority,
                            source={"partition_family": family},
                        )
                        _fail(failed, "stage2_mapping_group", error)
                        candidates.append(failed)
                        family_failures.append({
                            "case": case, "cores": cores,
                            "candidate": failed.name,
                            "error_type": failed.error_type,
                            "error": failed.error,
                        })

        existing_by_signature = {
            candidate.canonical_signature: candidate
            for candidate in candidates
            if candidate.legal and candidate.canonical_signature is not None
        }
        if cores >= 3 and previous_plan is not None and previous_group_hash:
            inherited = inherited_candidate(
                graph_json=graph_json,
                graph_hash=graph_hash,
                config_path=config_path,
                config_hash=config_hash,
                source_plan=previous_plan,
                source_cores=cores - 1,
                target_cores=cores,
                source_group_hash=previous_group_hash,
                priority=2 * len(MAPPING_FAMILIES) + 2,
                cache_dir=inherited_cache,
                implementation_hash=implementation_hash,
                solver_hash=solver_hash,
                existing_by_signature=existing_by_signature,
            )
            candidates.append(inherited)
            if not inherited.legal:
                family_failures.append({
                    "case": case, "cores": cores,
                    "candidate": inherited.name,
                    "error_type": inherited.error_type,
                    "error": inherited.error,
                })

        _deduplicate_candidates(candidates)
        eligible = [candidate for candidate in candidates
                    if candidate.legal and candidate.metrics is not None
                    and candidate.plan is not None and candidate.result is not None]
        if not eligible:
            raise Problem2FinalError("no legal official candidate remained")
        winner = min(eligible, key=lambda item: item.score())
        assert winner.plan is not None and winner.result is not None
        group_dir = output_root / "groups" / case / "k{}".format(cores)
        inherited_plan_dir = group_dir / "candidate_plans"
        for candidate in candidates:
            if (candidate.family != "recursive_final_winner_inheritance"
                    or candidate.plan is None):
                continue
            inherited_plan_path = inherited_plan_dir / (
                candidate.name + "_multicore_res.json")
            _write_json(inherited_plan_path, candidate.plan)
            candidate.plan_path = str(inherited_plan_path)
            candidate.plan_file_sha256 = sha256_file(inherited_plan_path)
        final_plan_path = group_dir / "final_multicore_res.json"
        final_result_path = group_dir / "final_official_evaluation.json"
        _write_json(final_plan_path, winner.plan)
        _write_json(final_result_path, winner.result)
        stage1_score = stage1.score()[:2]
        winner_score = winner.score()[:2]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "problem2_final_portfolio_group",
            "implementation_version": IMPLEMENTATION_VERSION,
            "status": "success",
            "case": case,
            "cores": cores,
            "run_identity_sha256": run_identity_sha256,
            "graph_sha256": graph_hash,
            "config_sha256": config_hash,
            "mapping_group_file_sha256": mapping_group_hashes,
            "candidate_priority": [item.name for item in sorted(
                candidates, key=lambda item: item.priority)],
            "candidates": [item.record() for item in sorted(
                candidates, key=lambda item: item.priority)],
            "failed_candidates": family_failures,
            "winner": {
                "name": winner.name,
                "family": winner.family,
                "score": list(winner_score),
                "metrics": copy.deepcopy(winner.metrics),
                "plan_hash": winner.plan_hash,
                "canonical_signature": winner.canonical_signature,
            },
            "stage1_fallback": {
                "winner": references[(case, cores)].winner,
                "score": list(stage1_score),
                "metrics": copy.deepcopy(stage1.metrics),
            },
            "paired_delta_vs_stage1": {
                "makespan_cycles": winner_score[0] - stage1_score[0],
                "added_copy_bytes": winner_score[1] - stage1_score[1],
                "spill_added_copy_bytes": (
                    int(winner.metrics["spill_added_copy_bytes"])
                    - int(stage1.metrics["spill_added_copy_bytes"])),
            },
            "final_plan_path": str(final_plan_path),
            "final_plan_hash": plan_json_sha256(winner.plan),
            "final_plan_file_sha256": sha256_file(final_plan_path),
            "final_official_result_path": str(final_result_path),
            "final_official_result_file_sha256": sha256_file(final_result_path),
            "final_official_result_json_sha256": json_sha256(winner.result),
            "timing": {
                "group_wall_seconds": time.perf_counter() - started,
            },
            "timestamp": _utc_now(),
        }
        _write_json(group_path, manifest)
        manifest["_resume"] = False
        groups.append(manifest)
        failures.extend(family_failures)
        previous_plan = copy.deepcopy(winner.plan)
        previous_group_hash = sha256_file(group_path)

    return groups, failures


__all__ = [
    "IMPLEMENTATION_VERSION",
    "MAPPING_POLICY",
    "Problem2FinalError",
    "final_implementation_hash",
    "reusable_final_group",
    "run_final_case",
]
