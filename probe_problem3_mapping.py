"""Resumable development-only probe of two shared-input one-core moves."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from audit_problem3_assets import sha256_file
from candidate_manager_problem3 import _cached_result, _save_cache, evaluation_key
from run_problem3_g1 import REPO, json_sha256, load_json, write_csv, write_json
from run_problem3_g2 import (
    completed_records as verified_g2_records,
    identity as current_g2_identity,
)
from solver_problem3_mapping import generate_shared_input_moves

sys.path.insert(0, str(REPO / "code"))
from evaluation_validation import read_evaluation_config
from multicore_cut_evaluate_problem_3 import (
    evaluate_problem_3, read_cache_config, read_scene_b_config,
)


SMOKE = ("case_001", "case_050", "case_100")


def completed_records(repo: Path, g1: Path, g2: Path, output: Path, ident: dict,
                      cases: list[str]) -> list[dict]:
    items = []
    for case in cases:
        for cores in (2, 3, 4, 5):
            group_dir = output / "groups" / case / f"k{cores}"
            path = group_dir / "group.json"
            if not path.is_file():
                raise ValueError(f"required earlier phase missing: {case}/k{cores}")
            record = load_json(path)
            if (record.get("status") != "success" or record.get("failures")
                    or record.get("case") != case or record.get("cores") != cores):
                raise ValueError(f"required earlier phase failed: {case}/k{cores}")
            source_dir = g2 / "groups" / case / f"k{cores}"
            verified_g2_records(
                repo, g1, g2, load_json(g2 / "run_identity.json"),
                [case], cores=(cores,))
            source_manifest = source_dir / "group.json"
            source_record = load_json(source_manifest)
            source_plan = source_dir / "final_multicore_res.json"
            source_result = source_dir / "final_official_evaluation.json"
            for source_file in (source_plan, source_result):
                name = source_file.name
                if sha256_file(source_file) != source_record["file_sha256"][name]:
                    raise ValueError(f"mapping source file damaged: {source_file}")
            expected_fingerprint = json_sha256({
                "identity": ident,
                "graph_sha256": sha256_file(repo / "data" / f"{case}.json"),
                "source_manifest_sha256": sha256_file(source_manifest),
                "baseline_plan_sha256": sha256_file(source_plan),
                "baseline_result_sha256": sha256_file(source_result),
            })
            if record.get("fingerprint") != expected_fingerprint:
                raise ValueError(f"required mapping identity changed: {case}/k{cores}")
            for name, digest in record["file_sha256"].items():
                file_path = group_dir / name
                if not file_path.is_file() or sha256_file(file_path) != digest:
                    raise ValueError(f"required mapping file damaged: {file_path}")
            baseline = load_json(group_dir / "official_results" / "baseline.json")
            final = load_json(group_dir / "final_official_evaluation.json")
            winner_record = next((item for item in record["candidates"]
                                  if item.get("name") == record["winner"]), None)
            if (json_sha256(load_json(group_dir / "plans" / "baseline.json"))
                    != json_sha256(load_json(source_plan))
                    or json_sha256(baseline) != json_sha256(load_json(source_result))
                    or baseline["makespan"] != record["baseline_makespan"]
                    or final["makespan"] != record["winner_makespan"]
                    or final["num_cores"] != cores
                    or record["strict_improvement"] != (
                        final["makespan"] < baseline["makespan"])
                    or winner_record is None
                    or winner_record.get("status") != "evaluated"
                    or json_sha256(load_json(group_dir / "final_multicore_res.json"))
                    != winner_record["plan_json_sha256"]
                    or json_sha256(final) != json_sha256(load_json(
                        group_dir / "official_results" /
                        f"{record['winner']}.json"))):
                raise ValueError(f"required mapping metric/winner mismatch: {case}/k{cores}")
            items.append(record)
    return items


def check_gate(repo: Path, g1: Path, g2: Path, output: Path, ident: dict,
               split: dict, phase: str) -> None:
    if phase == "smoke":
        return
    smoke = completed_records(repo, g1, g2, output, ident, list(SMOKE))
    if any(item["failures"] for item in smoke):
        raise ValueError("mapping smoke has candidate failures")
    if phase == "development":
        return
    development = completed_records(repo, g1, g2, output, ident,
                                    split["development_cases"])
    if any(item["failures"] for item in development):
        raise ValueError("mapping development has candidate failures")
    improved = {item["case"] for item in development
                if item["strict_improvement"]}
    if len(improved) < 2:
        raise ValueError("mapping branch stops: fewer than two development cases improve")
    if phase == "validation":
        return
    validation = completed_records(repo, g1, g2, output, ident,
                                   split["validation_cases"])
    if any(item["failures"] for item in validation):
        raise ValueError("mapping validation has candidate failures")
    improved = {item["case"] for item in validation
                if item["strict_improvement"]}
    if len(improved) < 2:
        raise ValueError("mapping branch stops: fewer than two validation cases improve")


def identity(repo: Path, g2: Path, g1: Path) -> dict:
    g2_ident = load_json(g2 / "run_identity.json")
    if g2_ident != current_g2_identity(repo, g1):
        raise ValueError("current G2 config, official files, implementation, or G1 identity differs")
    return {
        "schema_version": 1,
        "experiment": "problem3_shared_input_one_move_probe_v1",
        "g2_run_identity_file_sha256": sha256_file(g2 / "run_identity.json"),
        "config_lf_sha256": g2_ident["config_lf_sha256"],
        "official_file_lf_sha256": g2_ident["official_file_lf_sha256"],
        "implementation_file_sha256": {
            name: sha256_file(repo / name)
            for name in ("solver_problem3_mapping.py", "probe_problem3_mapping.py")
        },
    }


def source_group(repo: Path, g1: Path, g2: Path, case: str, cores: int):
    verified_g2_records(
        repo, g1, g2, load_json(g2 / "run_identity.json"),
        [case], cores=(cores,))
    directory = g2 / "groups" / case / f"k{cores}"
    manifest_path = directory / "group.json"
    manifest = load_json(manifest_path)
    if manifest.get("status") != "success":
        raise ValueError(f"G2 ordering group unsuccessful: {case}/k{cores}")
    plan_path = directory / "final_multicore_res.json"
    result_path = directory / "final_official_evaluation.json"
    graph_path = repo / "data" / f"{case}.json"
    for path in (plan_path, result_path):
        name = path.name
        if sha256_file(path) != manifest["file_sha256"][name]:
            raise ValueError(f"G2 source SHA256 mismatch: {path}")
    if sha256_file(graph_path) != manifest["identity"]["graph_sha256"]:
        raise ValueError(f"G2 graph SHA256 mismatch: {case}")
    return (load_json(graph_path), load_json(plan_path), load_json(result_path),
            graph_path, plan_path, result_path, manifest_path)


def run_group(repo, g1, g2, output, cache, case, cores, ident, settings):
    (graph, baseline_plan, baseline_result, graph_path,
         plan_path, result_path, source_manifest) = source_group(
         repo, g1, g2, case, cores)
    group_dir = output / "groups" / case / f"k{cores}"
    manifest_path = group_dir / "group.json"
    fingerprint = json_sha256({
        "identity": ident, "graph_sha256": sha256_file(graph_path),
        "source_manifest_sha256": sha256_file(source_manifest),
        "baseline_plan_sha256": sha256_file(plan_path),
        "baseline_result_sha256": sha256_file(result_path),
    })
    if manifest_path.is_file():
        saved = load_json(manifest_path)
        if saved.get("fingerprint") != fingerprint:
            raise ValueError(f"mapping probe identity mismatch: {case}/k{cores}")
        for name, digest in saved["file_sha256"].items():
            if sha256_file(group_dir / name) != digest:
                raise ValueError(f"mapping probe file SHA256 mismatch: {name}")
        return dict(saved, resumed=True, official_calls_this_invocation=0)
    started = time.perf_counter()
    group_dir.mkdir(parents=True, exist_ok=True)
    (group_dir / "plans").mkdir(exist_ok=True)
    (group_dir / "official_results").mkdir(exist_ok=True)
    shutil.copy2(plan_path, group_dir / "plans" / "baseline.json")
    shutil.copy2(result_path, group_dir / "official_results" / "baseline.json")
    records = [{"name": "baseline", "status": "evaluated",
                "makespan": baseline_result["makespan"],
                "plan_json_sha256": json_sha256(baseline_plan),
                "source": "verified_G2_ordering"}]
    winners = [("baseline", baseline_plan, baseline_result)]
    seen = {json_sha256(baseline_plan): "baseline"}
    official_calls = candidate_cache_hits = failures = 0
    try:
        proposals = generate_shared_input_moves(
            graph, baseline_plan, bandwidth=settings["bandwidth"],
            capacity=settings["capacity"],
            cross_core_delay=settings["cross_core_copy_delay_cycles"],
            cache_bandwidth=settings["cache_bandwidth_bytes_per_cycle"],
        )
    except Exception as error:
        proposals = {}
        failures += 1
        records.append({"name": "generation", "status": "failed",
                        "error_type": type(error).__name__, "error": str(error)})
    for name, (plan, diagnostics) in proposals.items():
        plan_file = group_dir / "plans" / f"{name}.json"
        write_json(plan_file, plan)
        write_json(group_dir / f"{name}_diagnostics.json", diagnostics)
        plan_hash = json_sha256(plan)
        record = {"name": name, "plan_json_sha256": plan_hash}
        if plan_hash in seen:
            record.update({"status": "deduplicated", "canonical": seen[plan_hash]})
            records.append(record)
            continue
        seen[plan_hash] = name
        key = evaluation_key(
            sha256_file(graph_path), ident["config_lf_sha256"], plan_hash,
            ident["official_file_lf_sha256"],
        )
        record["evaluation_key"] = key
        try:
            result = _cached_result(cache, key)
            if result is None:
                official_calls += 1
                result = evaluate_problem_3(
                    graph, plan, settings["bandwidth"], settings["capacity"],
                    settings["cross_core_copy_delay_cycles"],
                    settings["cache_capacity_bytes"],
                    settings["cache_bandwidth_bytes_per_cycle"],
                )
                _save_cache(cache, key, result)
                record["candidate_cache_hit"] = False
            else:
                candidate_cache_hits += 1
                record["candidate_cache_hit"] = True
            if result["num_cores"] != cores:
                raise ValueError("official result core count mismatch")
            result_file = group_dir / "official_results" / f"{name}.json"
            write_json(result_file, result)
            record.update({"status": "evaluated", "legal": True,
                           "makespan": result["makespan"],
                           "official_result_file_sha256": sha256_file(result_file)})
            winners.append((name, plan, result))
        except Exception as error:
            failures += 1
            record.update({"status": "failed", "legal": False,
                           "error_type": type(error).__name__, "error": str(error)})
        records.append(record)
    best_name, best_plan, best_result = min(
        winners, key=lambda item: (
            item[2]["makespan"],
            item[2]["data_movement_bytes"]["added_copy_bytes"], item[0]),
    )
    write_json(group_dir / "final_multicore_res.json", best_plan)
    write_json(group_dir / "final_official_evaluation.json", best_result)
    files = list((group_dir / "plans").glob("*.json")) + list(
        (group_dir / "official_results").glob("*.json")) + list(
        group_dir.glob("*_diagnostics.json")) + [
            group_dir / "final_multicore_res.json",
            group_dir / "final_official_evaluation.json",
        ]
    saved = {
        "schema_version": 1, "status": "success", "case": case,
        "cores": cores, "fingerprint": fingerprint,
        "winner": best_name,
        "baseline_makespan": baseline_result["makespan"],
        "winner_makespan": best_result["makespan"],
        "strict_improvement": best_result["makespan"] < baseline_result["makespan"],
        "candidates": records,
        "official_calls": official_calls,
        "candidate_cache_hits": candidate_cache_hits,
        "failures": failures,
        "elapsed_wall_seconds": time.perf_counter() - started,
        "file_sha256": {
            str(path.relative_to(group_dir)).replace("\\", "/"): sha256_file(path)
            for path in files
        },
    }
    write_json(manifest_path, saved)
    return dict(saved, resumed=False,
                official_calls_this_invocation=official_calls)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--g2", type=Path)
    parser.add_argument("--g1", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--phase", choices=(
        "smoke", "development", "validation", "holdout"), required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    g2 = (args.g2 or repo / "artifacts" / "problem3_g2_ordering").resolve()
    g1 = (args.g1 or repo / "artifacts" / "problem3_g1_paired").resolve()
    output = (args.output or repo / "artifacts" /
              "problem3_g2_mapping_probe").resolve()
    cache = (args.cache or repo / "artifacts" /
             "problem3_g2_mapping_cache").resolve()
    if len({g2, output, cache}) != 3:
        parser.error("G2 ordering, mapping output, and mapping cache must be distinct")
    split = load_json(repo / "problem3_preregistered_split.json")
    cases = list(SMOKE if args.phase == "smoke" else split[
        {"development": "development_cases", "validation": "validation_cases",
         "holdout": "holdout_cases"}[args.phase]])
    ident = identity(repo, g2, g1)
    output.mkdir(parents=True, exist_ok=True)
    ident_path = output / "run_identity.json"
    if ident_path.is_file() and load_json(ident_path) != ident:
        raise ValueError("mapping probe run identity changed")
    if not ident_path.is_file():
        write_json(ident_path, ident)
    check_gate(repo, g1, g2, output, ident, split, args.phase)
    config = repo / "data" / "config.txt"
    settings = read_evaluation_config(str(config))
    settings.update(read_scene_b_config(str(config)))
    settings.update(read_cache_config(str(config)))
    progress = []
    started = time.perf_counter()
    for case in cases:
        for cores in (2, 3, 4, 5):
            try:
                item = run_group(repo, g1, g2, output, cache, case, cores,
                                 ident, settings)
                progress.append({
                    "case": case, "cores": cores, "status": "success",
                    "winner": item["winner"],
                    "strict_improvement": item["strict_improvement"],
                    "official_calls": item["official_calls_this_invocation"],
                    "candidate_cache_hits": (0 if item["resumed"] else
                                             item["candidate_cache_hits"]),
                    "failures": item["failures"], "resumed": item["resumed"],
                })
                print(f"{case}/k{cores} {item['baseline_makespan']}->"
                      f"{item['winner_makespan']} winner={item['winner']} "
                      f"calls={item['official_calls_this_invocation']}",
                      flush=True)
            except Exception as error:
                progress.append({"case": case, "cores": cores,
                                 "status": "failed",
                                 "error_type": type(error).__name__,
                                 "error": str(error)})
                print(f"{case}/k{cores} FAILED: {error}", file=sys.stderr,
                      flush=True)
    rows = []
    for path in sorted((output / "groups").glob("case_*/k*/group.json")):
        item = load_json(path)
        rows.append({key: item[key] for key in (
            "case", "cores", "winner", "baseline_makespan",
            "winner_makespan", "strict_improvement", "official_calls",
            "candidate_cache_hits", "failures")})
    write_csv(output / "candidate_results.csv", rows,
              list(rows[0]) if rows else ["case", "cores"])
    invocation = {
        "schema_version": 1, "phase": args.phase, "progress": progress,
        "failed_groups": sum(item["status"] == "failed" for item in progress),
        "official_calls_this_invocation": sum(
            item.get("official_calls", 0) for item in progress),
        "strict_improvement_groups": sum(
            item.get("strict_improvement", False) for item in progress),
        "strict_improvement_cases": sorted({
            item["case"] for item in progress
            if item.get("strict_improvement", False)}),
        "elapsed_wall_seconds": time.perf_counter() - started,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    write_json(output / "invocations" / f"{stamp}.json", invocation)
    return 1 if invocation["failed_groups"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
