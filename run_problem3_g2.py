"""Gated, resumable Problem-3 ordering experiments on frozen G1 plans."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from audit_problem3_assets import DEPENDENCIES, sha256_file
from candidate_manager_problem3 import run_candidate_group
from run_problem3_g1 import (
    REPO, baseline_preflight, identity as g1_identity,
    json_sha256, load_json, write_csv, write_json,
)

sys.path.insert(0, str(REPO / "code"))
from evaluation_validation import read_evaluation_config
from multicore_cut_evaluate_problem_3 import read_cache_config, read_scene_b_config


SMOKE_CASES = ("case_001", "case_050", "case_100")
IMPLEMENTATION_FILES = (
    "audit_problem3_assets.py", "run_problem3_g1.py",
    "solver_problem1.py", "solver_problem2.py", "solver_problem2_stage3.py",
    "solver_problem3.py", "candidate_manager_problem3.py",
    "run_problem3_g2.py", "problem3_preregistered_split.json",
)


def identity(repo: Path, g1: Path) -> dict:
    code = repo / "code"
    return {
        "schema_version": 1, "experiment": "problem3_g2_reuse_window_v1",
        "config_lf_sha256": sha256_file(repo / "data" / "config.txt",
                                        normalize_newlines=True),
        "official_file_lf_sha256": {
            name: sha256_file(code / name, normalize_newlines=True)
            for name in DEPENDENCIES
        },
        "implementation_file_sha256": {
            name: sha256_file(repo / name)
            for name in IMPLEMENTATION_FILES
        },
        "g1_identity_file_sha256": sha256_file(g1 / "run_identity.json"),
    }


def phase_cases(split: dict, phase: str) -> list[str]:
    if phase == "smoke":
        return list(SMOKE_CASES)
    key = {
        "development": "development_cases",
        "validation": "validation_cases",
        "holdout": "holdout_cases",
    }[phase]
    return list(split[key])


def completed_records(repo: Path, g1: Path, output: Path, ident: dict,
                      cases: list[str], cores=(2, 3, 4, 5)) -> list[dict]:
    records = []
    for case in cases:
        for core in cores:
            group_dir = output / "groups" / case / f"k{core}"
            path = group_dir / "group.json"
            if not path.is_file():
                raise ValueError(f"required earlier phase group missing: {case}/k{core}")
            record = load_json(path)
            if (record.get("status") != "success" or record.get("case") != case
                    or record.get("cores") != core):
                raise ValueError(f"required earlier phase group failed: {case}/k{core}")
            g1_record = load_json(g1 / "groups" / case / f"k{core}" / "group.json")
            expected_identity = {
                "schema_version": 1, "case": case, "cores": core,
                "graph_sha256": sha256_file(repo / "data" / f"{case}.json"),
                "config_lf_sha256": ident["config_lf_sha256"],
                "official_file_lf_sha256": ident["official_file_lf_sha256"],
                "implementation_file_sha256": ident["implementation_file_sha256"],
                "baseline_plan_json_sha256": g1_record["plan_json_sha256"],
                "baseline_l2_json_sha256": g1_record["l2_result_json_sha256"],
                "baseline_g1_manifest_sha256": sha256_file(
                    g1 / "groups" / case / f"k{core}" / "group.json"),
            }
            if (record.get("identity") != expected_identity or
                    record.get("fingerprint") != json_sha256(expected_identity)):
                raise ValueError(f"required earlier phase identity changed: {case}/k{core}")
            for name, digest in record["file_sha256"].items():
                file_path = group_dir / name
                if not file_path.is_file() or sha256_file(file_path) != digest:
                    raise ValueError(f"required earlier phase file damaged: {file_path}")
            baseline = load_json(group_dir / "official_results" / "baseline.json")
            final = load_json(group_dir / "final_official_evaluation.json")
            if (json_sha256(load_json(group_dir / "plans" / "baseline.json"))
                    != expected_identity["baseline_plan_json_sha256"]
                    or json_sha256(baseline) != expected_identity["baseline_l2_json_sha256"]
                    or baseline["makespan"] != record["baseline_makespan"]
                    or final["makespan"] != record["winner_makespan"]
                    or final["num_cores"] != core
                    or record["strict_improvement"] != (
                        final["makespan"] < baseline["makespan"])):
                raise ValueError(f"required earlier phase metric mismatch: {case}/k{core}")
            winner_record = next((item for item in record["candidates"]
                                  if item.get("name") == record["winner"]), None)
            if (winner_record is None or winner_record.get("status") != "evaluated"
                    or json_sha256(load_json(group_dir / "final_multicore_res.json"))
                    != winner_record["plan_json_sha256"]
                    or json_sha256(final) != winner_record["official_result_json_sha256"]):
                raise ValueError(f"required earlier phase winner mismatch: {case}/k{core}")
            records.append(record)
    return records


def check_gate(repo: Path, g1: Path, output: Path, ident: dict,
               split: dict, phase: str) -> None:
    if phase == "smoke":
        return
    smoke = completed_records(repo, g1, output, ident, list(SMOKE_CASES))
    if any(item["generation_failures"] or item["evaluation_failures"]
           for item in smoke):
        raise ValueError("smoke has candidate failures; resolve before extension")
    if phase == "development":
        return
    development = completed_records(repo, g1, output, ident,
                                    split["development_cases"])
    if any(item["generation_failures"] or item["evaluation_failures"]
           for item in development):
        raise ValueError("development has candidate failures; resolve before extension")
    improved_dev = {item["case"] for item in development
                    if item["strict_improvement"]}
    if len(improved_dev) < 2:
        raise ValueError(
            "G2 stop: ordering improved fewer than two development cases")
    if phase == "validation":
        return
    validation = completed_records(repo, g1, output, ident,
                                   split["validation_cases"])
    if any(item["generation_failures"] or item["evaluation_failures"]
           for item in validation):
        raise ValueError("validation has candidate failures; resolve before holdout")
    improved_validation = {item["case"] for item in validation
                           if item["strict_improvement"]}
    if len(improved_validation) < 2:
        raise ValueError(
            "G2 stop: ordering improved fewer than two validation cases")


def load_g1_group(repo: Path, g1: Path, case: str, cores: int):
    group_dir = g1 / "groups" / case / f"k{cores}"
    path = group_dir / "group.json"
    record = load_json(path)
    if record.get("status") != "success":
        raise ValueError(f"G1 baseline is not successful: {case}/k{cores}")
    plan_path = group_dir / "winner_plan.json"
    result_path = group_dir / "l2_official.json"
    graph_path = repo / "data" / f"{case}.json"
    for file_path, expected in (
        (plan_path, record["plan_file_sha256"]),
        (result_path, record["l2_result_file_sha256"]),
        (graph_path, record["graph_sha256"]),
    ):
        if sha256_file(file_path) != expected:
            raise ValueError(f"G1 source file SHA256 mismatch: {file_path}")
    plan, result = load_json(plan_path), load_json(result_path)
    if json_sha256(plan) != record["plan_json_sha256"]:
        raise ValueError(f"G1 plan JSON hash mismatch: {case}/k{cores}")
    if json_sha256(result) != record["l2_result_json_sha256"]:
        raise ValueError(f"G1 result JSON hash mismatch: {case}/k{cores}")
    return load_json(graph_path), plan, result, plan_path, result_path, path


def summarize(output: Path) -> dict:
    rows = []
    for path in sorted((output / "groups").glob("case_*/k*/group.json")):
        item = load_json(path)
        if item.get("status") != "success":
            continue
        rows.append({
            "case": item["case"], "cores": item["cores"],
            "baseline_l2_makespan": item["baseline_makespan"],
            "winner_l2_makespan": item["winner_makespan"],
            "winner": item["winner"],
            "strict_improvement": item["strict_improvement"],
            "l2_ordering_speedup": (item["baseline_makespan"] /
                                    item["winner_makespan"]),
            "official_calls": item["official_calls"],
            "candidate_cache_hits": item["candidate_cache_hits"],
            "generation_failures": item["generation_failures"],
            "evaluation_failures": item["evaluation_failures"],
        })
    fields = list(rows[0]) if rows else ["case", "cores"]
    write_csv(output / "candidate_results.csv", rows, fields)
    return {
        "record_count": len(rows),
        "strict_improvement_groups": sum(row["strict_improvement"] for row in rows),
        "strict_improvement_cases": len({row["case"] for row in rows
                                         if row["strict_improvement"]}),
        "total_official_calls": sum(row["official_calls"] for row in rows),
        "total_candidate_cache_hits": sum(row["candidate_cache_hits"] for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--g1", type=Path)
    parser.add_argument("--baseline", type=Path,
                        help="Stage-1 directory used to validate reused no-L2 results")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--phase", choices=(
        "smoke", "development", "validation", "holdout"), required=True)
    parser.add_argument("--cores", nargs="+", type=int, default=[2, 3, 4, 5])
    args = parser.parse_args()
    repo = args.repo.resolve()
    g1 = (args.g1 or repo / "artifacts" / "problem3_g1_paired").resolve()
    baseline = (args.baseline or repo / "artifacts" /
                "problem2_stage1_baseline_full").resolve()
    output = (args.output or repo / "artifacts" / "problem3_g2_ordering").resolve()
    cache = (args.cache or repo / "artifacts" / "problem3_g2_candidate_cache").resolve()
    if args.cores != [2, 3, 4, 5]:
        parser.error("frozen G2 design requires --cores 2 3 4 5")
    if output == cache or output == g1 or cache == g1:
        parser.error("G1 output, G2 output, and G2 cache must be distinct")
    split = load_json(repo / "problem3_preregistered_split.json")
    if (len(split["development_cases"]), len(split["validation_cases"]),
            len(split["holdout_cases"])) != (12, 18, 70):
        raise ValueError("preregistered split has wrong case counts")
    all_cases = (split["development_cases"] + split["validation_cases"]
                 + split["holdout_cases"])
    if len(set(all_cases)) != 100:
        raise ValueError("preregistered split is not disjoint")
    g1_ident = g1_identity(repo)
    baseline_preflight(repo, baseline, g1_ident)
    if load_json(g1 / "run_identity.json") != g1_ident:
        raise ValueError("G1 baseline run identity differs from current code")
    ident = identity(repo, g1)
    output.mkdir(parents=True, exist_ok=True)
    identity_path = output / "run_identity.json"
    if identity_path.exists() and load_json(identity_path) != ident:
        raise ValueError("G2 output identity changed; use a new output/cache namespace")
    if not identity_path.exists():
        write_json(identity_path, ident)
    check_gate(repo, g1, output, ident, split, args.phase)
    config_path = repo / "data" / "config.txt"
    settings = read_evaluation_config(str(config_path))
    settings.update(read_scene_b_config(str(config_path)))
    settings.update(read_cache_config(str(config_path)))
    cases = phase_cases(split, args.phase)
    progress = []
    started = time.perf_counter()
    for case in cases:
        for cores in args.cores:
            try:
                (graph, plan, l2, plan_path, l2_path,
                 g1_manifest_path) = load_g1_group(repo, g1, case, cores)
                record = run_candidate_group(
                    graph=graph,
                    graph_hash=sha256_file(repo / "data" / f"{case}.json"),
                    baseline_plan=plan, baseline_l2=l2,
                    baseline_plan_path=plan_path,
                    baseline_l2_path=l2_path,
                    baseline_g1_manifest_path=g1_manifest_path,
                    case=case, cores=cores, settings=settings,
                    config_lf_hash=ident["config_lf_sha256"],
                    official_hashes=ident["official_file_lf_sha256"],
                    implementation_hashes=ident["implementation_file_sha256"],
                    group_dir=output / "groups" / case / f"k{cores}",
                    cache_dir=cache,
                )
                progress.append({
                    "case": case, "cores": cores, "status": "success",
                    "winner": record["winner"],
                    "strict_improvement": record["strict_improvement"],
                    "official_calls": record["official_calls_this_invocation"],
                    "candidate_cache_hits": (0 if record["resumed"] else
                                             record["candidate_cache_hits"]),
                    "resumed": record["resumed"],
                    "generation_failures": record["generation_failures"],
                    "evaluation_failures": record["evaluation_failures"],
                })
                print(f"{case}/k{cores} winner={record['winner']} "
                      f"{record['baseline_makespan']}->{record['winner_makespan']} "
                      f"calls={record['official_calls_this_invocation']} "
                      f"{'resume' if record['resumed'] else 'evaluated'}",
                      flush=True)
            except Exception as error:
                progress.append({
                    "case": case, "cores": cores, "status": "failed",
                    "error_type": type(error).__name__, "error": str(error),
                })
                print(f"{case}/k{cores} FAILED {type(error).__name__}: {error}",
                      file=sys.stderr, flush=True)
    summary = summarize(output)
    invocation = {
        "schema_version": 1, "phase": args.phase,
        "cases": cases, "cores": args.cores,
        "progress": progress,
        "official_calls_this_invocation": sum(
            item.get("official_calls", 0) for item in progress),
        "candidate_cache_hits_this_invocation": sum(
            item.get("candidate_cache_hits", 0) for item in progress),
        "failed_groups": sum(item["status"] == "failed" for item in progress),
        "elapsed_wall_seconds": time.perf_counter() - started,
        "summary": summary,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    write_json(output / "invocations" / f"{stamp}.json", invocation)
    return 1 if invocation["failed_groups"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
