"""Content-addressed, isolated official evaluation of Problem-3 candidates."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

from audit_problem3_assets import sha256_file
from run_problem3_g1 import compact_metrics, json_sha256, load_json, write_json
from solver_problem3 import generate_reuse_window_candidates

sys.path.insert(0, str(Path(__file__).resolve().parent / "code"))
from multicore_cut_evaluate_problem_3 import evaluate_problem_3


def evaluation_key(graph_hash, config_hash, plan_hash, official_hashes) -> str:
    """Problem-3-only namespace; any official dependency change invalidates it."""
    return json_sha256({
        "namespace": "problem3_l2_official_v1",
        "graph_sha256": graph_hash,
        "config_lf_sha256": config_hash,
        "plan_json_sha256": plan_hash,
        "official_file_lf_sha256": official_hashes,
    })


def _cached_result(cache: Path, key: str):
    entry_path = cache / "entries" / f"{key}.json"
    result_path = cache / "results" / f"{key}.json"
    if not entry_path.exists() or not result_path.exists():
        return None
    entry = load_json(entry_path)
    if entry.get("evaluation_key") != key:
        raise ValueError(f"Problem-3 cache key mismatch: {key}")
    if sha256_file(result_path) != entry.get("result_file_sha256"):
        raise ValueError(f"Problem-3 cache file SHA256 mismatch: {key}")
    result = load_json(result_path)
    if json_sha256(result) != entry.get("result_json_sha256"):
        raise ValueError(f"Problem-3 cache JSON SHA256 mismatch: {key}")
    return result


def _save_cache(cache: Path, key: str, result: dict) -> None:
    result_path = cache / "results" / f"{key}.json"
    entry_path = cache / "entries" / f"{key}.json"
    write_json(result_path, result)
    write_json(entry_path, {
        "schema_version": 1, "evaluation_key": key,
        "result_file_sha256": sha256_file(result_path),
        "result_json_sha256": json_sha256(result),
        "makespan": result["makespan"],
    })


def run_candidate_group(
    *,
    graph: dict,
    graph_hash: str,
    baseline_plan: dict,
    baseline_l2: dict,
    baseline_plan_path: Path,
    baseline_l2_path: Path,
    baseline_g1_manifest_path: Path,
    case: str,
    cores: int,
    settings: dict,
    config_lf_hash: str,
    official_hashes: dict,
    implementation_hashes: dict,
    group_dir: Path,
    cache_dir: Path,
) -> dict:
    """Keep the frozen baseline, evaluate unique orderings, retain best result."""
    started = time.perf_counter()
    group_dir = Path(group_dir)
    cache_dir = Path(cache_dir)
    manifest_path = group_dir / "group.json"
    baseline_plan_hash = json_sha256(baseline_plan)
    baseline_result_hash = json_sha256(baseline_l2)
    identity = {
        "schema_version": 1, "case": case, "cores": cores,
        "graph_sha256": graph_hash,
        "config_lf_sha256": config_lf_hash,
        "official_file_lf_sha256": official_hashes,
        "implementation_file_sha256": implementation_hashes,
        "baseline_plan_json_sha256": baseline_plan_hash,
        "baseline_l2_json_sha256": baseline_result_hash,
        "baseline_g1_manifest_sha256": sha256_file(baseline_g1_manifest_path),
    }
    fingerprint = json_sha256(identity)
    if manifest_path.exists():
        saved = load_json(manifest_path)
        if saved.get("fingerprint") != fingerprint:
            raise ValueError(f"Problem-3 G2 resume identity mismatch: {case}/k{cores}")
        for name, hash_value in saved.get("file_sha256", {}).items():
            path = group_dir / name
            if not path.is_file() or sha256_file(path) != hash_value:
                raise ValueError(f"Problem-3 G2 resume file damaged: {path}")
        if saved.get("status") != "success":
            raise ValueError(f"Problem-3 G2 resume status invalid: {case}/k{cores}")
        return dict(saved, resumed=True, official_calls_this_invocation=0)

    group_dir.mkdir(parents=True, exist_ok=True)
    (group_dir / "plans").mkdir(exist_ok=True)
    (group_dir / "official_results").mkdir(exist_ok=True)
    shutil.copy2(baseline_plan_path, group_dir / "plans" / "baseline.json")
    shutil.copy2(baseline_l2_path, group_dir / "official_results" / "baseline.json")
    baseline_metrics = compact_metrics(baseline_l2, l2=True)
    records = [{
        "name": "baseline", "status": "evaluated", "legal": True,
        "plan_json_sha256": baseline_plan_hash,
        "official_result_json_sha256": baseline_result_hash,
        "source": "verified_G1", "official_call": False,
        "candidate_cache_hit": False,
        "metrics": baseline_metrics,
    }]
    winners = [("baseline", baseline_plan, baseline_l2)]
    seen = {baseline_plan_hash: "baseline"}
    official_calls = cache_hits = generation_failures = evaluation_failures = 0
    try:
        proposals = generate_reuse_window_candidates(
            graph, baseline_plan,
            bandwidth=settings["bandwidth"], capacity=settings["capacity"],
            cross_core_delay=settings["cross_core_copy_delay_cycles"],
            cache_capacity_bytes=settings["cache_capacity_bytes"],
            cache_bandwidth_bytes_per_cycle=settings["cache_bandwidth_bytes_per_cycle"],
        )
    except Exception as error:
        generation_failures += 1
        proposals = {}
        records.append({
            "name": "ordering_generation", "status": "failed",
            "failure_stage": "generation", "error_type": type(error).__name__,
            "error": str(error), "official_call": False,
        })
    for name, (plan, diagnostics) in proposals.items():
        plan_hash = json_sha256(plan)
        plan_path = group_dir / "plans" / f"{name}.json"
        write_json(plan_path, plan)
        write_json(group_dir / f"{name}_diagnostics.json", diagnostics)
        record = {
            "name": name, "plan_json_sha256": plan_hash,
            "plan_file_sha256": sha256_file(plan_path),
            "order_changed": diagnostics["order_changed"],
            "official_call": False, "candidate_cache_hit": False,
        }
        if plan_hash in seen:
            record.update({"status": "deduplicated", "legal": True,
                           "canonical_candidate": seen[plan_hash]})
            records.append(record)
            continue
        seen[plan_hash] = name
        key = evaluation_key(graph_hash, config_lf_hash, plan_hash,
                             official_hashes)
        record["evaluation_key"] = key
        try:
            result = _cached_result(cache_dir, key)
            if result is None:
                official_calls += 1
                record["official_call"] = True
                result = evaluate_problem_3(
                    graph, plan, settings["bandwidth"], settings["capacity"],
                    settings["cross_core_copy_delay_cycles"],
                    settings["cache_capacity_bytes"],
                    settings["cache_bandwidth_bytes_per_cycle"],
                )
                _save_cache(cache_dir, key, result)
            else:
                cache_hits += 1
                record["candidate_cache_hit"] = True
            if result["num_cores"] != cores:
                raise ValueError("official result core count differs")
            official_path = group_dir / "official_results" / f"{name}.json"
            write_json(official_path, result)
            record.update({
                "status": "evaluated", "legal": True,
                "official_result_json_sha256": json_sha256(result),
                "official_result_file_sha256": sha256_file(official_path),
                "metrics": compact_metrics(result, l2=True),
            })
            winners.append((name, plan, result))
        except Exception as error:
            evaluation_failures += 1
            record.update({
                "status": "failed", "legal": False,
                "failure_stage": "official_evaluation_or_cache",
                "error_type": type(error).__name__, "error": str(error),
            })
        records.append(record)
    best_name, best_plan, best_result = min(
        winners,
        key=lambda item: (
            item[2]["makespan"],
            item[2]["data_movement_bytes"]["added_copy_bytes"],
            item[2]["data_movement_bytes"]["scheduled_copy_bytes"],
            item[0],
        ),
    )
    winner_plan_path = group_dir / "final_multicore_res.json"
    winner_result_path = group_dir / "final_official_evaluation.json"
    write_json(winner_plan_path, best_plan)
    write_json(winner_result_path, best_result)
    files = (
        list((group_dir / "plans").glob("*.json"))
        + list((group_dir / "official_results").glob("*.json"))
        + list(group_dir.glob("*_diagnostics.json"))
        + [winner_plan_path, winner_result_path]
    )
    saved = {
        "schema_version": 1, "status": "success",
        "case": case, "cores": cores, "fingerprint": fingerprint,
        "identity": identity,
        "winner": best_name,
        "baseline_makespan": baseline_l2["makespan"],
        "winner_makespan": best_result["makespan"],
        "strict_improvement": best_result["makespan"] < baseline_l2["makespan"],
        "candidates": records,
        "official_calls": official_calls,
        "candidate_cache_hits": cache_hits,
        "generation_failures": generation_failures,
        "evaluation_failures": evaluation_failures,
        "elapsed_wall_seconds": time.perf_counter() - started,
        "file_sha256": {
            str(path.relative_to(group_dir)).replace("\\", "/"): sha256_file(path)
            for path in files
        },
    }
    write_json(manifest_path, saved)
    return dict(saved, resumed=False,
                official_calls_this_invocation=official_calls)
