"""Resumable paired no-L2/L2 evaluation of frozen Problem-2 Stage-1 winners.

Runs sequentially so one process owns each output directory. The supplied
evaluators and data/config.txt are read without modification.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from audit_problem3_assets import (
    DEPENDENCIES, internal_plan_sha256, serialized_plan_sha256, sha256_file,
)


REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "code"))

from evaluation_validation import read_evaluation_config
from multicore_cut_evaluate_problem_2 import evaluate_scene_b
from multicore_cut_evaluate_problem_3 import (
    evaluate_problem_3, read_cache_config, read_scene_b_config,
)


def json_sha256(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields}
                         for row in rows)
    temporary.replace(path)


def git_head(repo: Path) -> str | None:
    probe = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=False,
    )
    return probe.stdout.strip() if probe.returncode == 0 else None


def identity(repo: Path) -> dict:
    code = repo / "code"
    files = {name: sha256_file(code / name, normalize_newlines=True)
             for name in DEPENDENCIES}
    implementation = {
        name: sha256_file(repo / name)
        for name in ("audit_problem3_assets.py", "run_problem3_g1.py")
    }
    return {
        "schema_version": 1, "experiment": "problem3_g1_paired",
        "config_sha256": sha256_file(repo / "data" / "config.txt",
                                     normalize_newlines=True),
        "official_file_lf_sha256": files,
        "implementation_file_sha256": implementation,
    }


def baseline_preflight(repo: Path, baseline: Path, ident: dict) -> dict:
    archived = load_json(baseline / "run_identity.json")
    if ident["config_sha256"] != archived["config_sha256"]:
        raise ValueError("Stage-1 configuration differs from current config")
    current = ident["official_file_lf_sha256"]
    for name, old_hash in archived["evaluator_file_sha256"].items():
        if current[name] != old_hash:
            raise ValueError(f"Stage-1 official dependency changed: {name}")
    return archived


def baseline_group(repo: Path, baseline: Path, case: str, cores: int):
    manifest_path = baseline / "groups" / case / f"k{cores}.json"
    group_dir = baseline / "groups" / case / f"k{cores}"
    plan_path = group_dir / "final_multicore_res.json"
    result_path = group_dir / "final_official_evaluation.json"
    manifest = load_json(manifest_path)
    if manifest.get("status") != "success":
        raise ValueError(f"Stage-1 group did not succeed: {case}/k{cores}")
    graph_path = repo / "data" / f"{case}.json"
    checks = (
        (graph_path, manifest["graph_sha256"]),
        (plan_path, manifest["final_plan_file_sha256"]),
        (result_path, manifest["final_official_result_file_sha256"]),
    )
    for path, expected in checks:
        if sha256_file(path) != expected:
            raise ValueError(f"Stage-1 asset SHA256 mismatch: {path}")
    plan, result = load_json(plan_path), load_json(result_path)
    if manifest["winner"]["plan_hash"] not in (
            internal_plan_sha256(plan), serialized_plan_sha256(plan)):
        raise ValueError(f"Stage-1 plan canonical hash mismatch: {case}/k{cores}")
    if json_sha256(result) != manifest["final_official_result_json_sha256"]:
        raise ValueError(f"Stage-1 result canonical hash mismatch: {case}/k{cores}")
    if result["num_cores"] != cores:
        raise ValueError(f"Stage-1 result core count mismatch: {case}/k{cores}")
    return graph_path, plan_path, result_path, manifest, plan, result


def compact_metrics(result: dict, *, l2: bool) -> dict:
    movement = result["data_movement_bytes"]
    cache = result.get("cache_stats", {}) if l2 else {}
    return {
        "makespan": result["makespan"],
        "added_copy_bytes": movement["added_copy_bytes"],
        "scheduled_copy_bytes": movement["scheduled_copy_bytes"],
        "spill_added_copy_bytes": movement["spill_added_copy_bytes"],
        "partition_added_copy_bytes": movement["partition_added_copy_bytes"],
        "cross_task_traffic_bytes": result["cross_task_traffic"],
        "cross_core_transfer_count": len(result["cross_core_transfers"]),
        "copy_in_hits": cache.get("copy_in_hits"),
        "copy_in_misses": cache.get("copy_in_misses"),
        "cache_hit_bytes": cache.get("hit_bytes"),
        "cache_miss_bytes": cache.get("miss_bytes"),
        "cache_hit_rate_bytes": cache.get("hit_rate"),
    }


def group_fingerprint(ident, graph_hash, plan_hash, p2_result_hash) -> str:
    return json_sha256({
        "identity": ident, "graph_sha256": graph_hash,
        "plan_sha256": plan_hash, "p2_result_sha256": p2_result_hash,
    })


def run_group(repo, baseline, output, case, cores, ident, settings,
              *, recheck_no_l2=False) -> tuple[dict, bool, int]:
    (graph_path, plan_path, old_result_path,
     old_manifest, plan, old_result) = baseline_group(
         repo, baseline, case, cores,
    )
    fingerprint = group_fingerprint(
        ident, old_manifest["graph_sha256"],
        old_manifest["final_plan_file_sha256"],
        old_manifest["final_official_result_file_sha256"],
    )
    group_dir = output / "groups" / case / f"k{cores}"
    manifest_path = group_dir / "group.json"
    local_plan = group_dir / "winner_plan.json"
    local_p2 = group_dir / "no_l2_official.json"
    local_p3 = group_dir / "l2_official.json"
    if manifest_path.is_file():
        saved = load_json(manifest_path)
        if saved.get("fingerprint") != fingerprint:
            raise ValueError(f"G1 resume identity mismatch: {case}/k{cores}")
        for path, key in (
            (local_plan, "plan_file_sha256"),
            (local_p2, "no_l2_result_file_sha256"),
            (local_p3, "l2_result_file_sha256"),
        ):
            if not path.is_file() or sha256_file(path) != saved.get(key):
                raise ValueError(f"G1 resume file damaged: {path}")
        if saved.get("status") != "success":
            raise ValueError(f"G1 resume status invalid: {case}/k{cores}")
        if saved.get("plan_json_sha256") != json_sha256(load_json(local_plan)):
            raise ValueError(f"G1 resume plan JSON hash mismatch: {case}/k{cores}")
        for label, path in (("no_l2", local_p2), ("l2", local_p3)):
            result = load_json(path)
            if saved.get(f"{label}_result_json_sha256") != json_sha256(result):
                raise ValueError(f"G1 resume result JSON hash mismatch: {case}/k{cores}/{label}")
            if saved.get(label) != compact_metrics(result, l2=label == "l2"):
                raise ValueError(f"G1 resume metrics mismatch: {case}/k{cores}/{label}")
        return saved, True, 0

    started = time.perf_counter()
    graph = load_json(graph_path)
    bandwidth = settings["bandwidth"]
    capacity = settings["capacity"]
    delay = settings["cross_core_copy_delay_cycles"]
    p2_calls = 0
    if recheck_no_l2:
        no_l2 = evaluate_scene_b(graph, plan, bandwidth, capacity, delay)
        p2_calls = 1
        if compact_metrics(no_l2, l2=False) != compact_metrics(old_result, l2=False):
            raise ValueError(f"independent no-L2 recheck differs: {case}/k{cores}")
    else:
        no_l2 = old_result
    l2 = evaluate_problem_3(
        graph, plan, bandwidth, capacity, delay,
        settings["cache_capacity_bytes"],
        settings["cache_bandwidth_bytes_per_cycle"],
    )
    if l2["num_cores"] != cores:
        raise ValueError(f"official L2 result core mismatch: {case}/k{cores}")
    if l2["data_movement_bytes"] != no_l2["data_movement_bytes"]:
        raise ValueError(f"paired data movement differs: {case}/k{cores}")
    group_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan_path, local_plan)
    if recheck_no_l2:
        write_json(local_p2, no_l2)
    else:
        shutil.copy2(old_result_path, local_p2)
    write_json(local_p3, l2)
    p2_metrics = compact_metrics(no_l2, l2=False)
    p3_metrics = compact_metrics(l2, l2=True)
    saved = {
        "schema_version": 1, "status": "success",
        "case": case, "cores": cores,
        "fingerprint": fingerprint,
        "graph_sha256": old_manifest["graph_sha256"],
        "plan_file_sha256": sha256_file(local_plan),
        "plan_json_sha256": json_sha256(plan),
        "no_l2_result_file_sha256": sha256_file(local_p2),
        "l2_result_file_sha256": sha256_file(local_p3),
        "no_l2_result_json_sha256": json_sha256(no_l2),
        "l2_result_json_sha256": json_sha256(l2),
        "no_l2_source": "official_recheck" if recheck_no_l2 else "stage1_verified_reuse",
        "git_head_at_execution": git_head(repo),
        "no_l2": p2_metrics, "l2": p3_metrics,
        "same_core_speedup": p2_metrics["makespan"] / p3_metrics["makespan"],
        "official_calls": {"problem_2": p2_calls, "problem_3": 1},
        "candidate_cache_hit_count": 0,
        "elapsed_wall_seconds": time.perf_counter() - started,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(manifest_path, saved)
    return saved, False, p2_calls + 1


def summarize(output: Path) -> dict:
    records = []
    for path in sorted((output / "groups").glob("case_*/k*/group.json")):
        record = load_json(path)
        if record.get("status") != "success":
            continue
        row = {
            "case": record["case"], "cores": record["cores"],
            "plan_sha256": record["plan_file_sha256"],
            "graph_sha256": record["graph_sha256"],
            "no_l2_source": record["no_l2_source"],
            "same_core_speedup": record["same_core_speedup"],
        }
        for prefix in ("no_l2", "l2"):
            row.update({f"{prefix}_{key}": value
                        for key, value in record[prefix].items()})
        records.append(row)
    fields = list(records[0]) if records else ["case", "cores"]
    write_csv(output / "paired_results.csv", records, fields)
    curves = []
    for cores in range(1, 6):
        subset = [row for row in records if row["cores"] == cores]
        if not subset:
            continue
        p2 = [row["no_l2_makespan"] for row in subset]
        p3 = [row["l2_makespan"] for row in subset]
        ratios = [row["same_core_speedup"] for row in subset]
        curves.append({
            "cores": cores, "cases": len(subset),
            "mean_no_l2_makespan": statistics.mean(p2),
            "mean_l2_makespan": statistics.mean(p3),
            "mean_of_case_speedup_ratios": statistics.mean(ratios),
            "median_of_case_speedup_ratios": statistics.median(ratios),
            "ratio_of_mean_makespans": statistics.mean(p2) / statistics.mean(p3),
            "l2_faster_cases": sum(a > b for a, b in zip(p2, p3)),
            "l2_slower_cases": sum(a < b for a, b in zip(p2, p3)),
        })
    curve_fields = list(curves[0]) if curves else ["cores", "cases"]
    write_csv(output / "curve.csv", curves, curve_fields)
    return {"record_count": len(records), "curve": curves}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cases", nargs="+", default=None)
    parser.add_argument("--cores", nargs="+", type=int, default=[1, 2, 3, 4, 5])
    parser.add_argument("--recheck-no-l2", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    baseline = (args.baseline or
                repo / "artifacts" / "problem2_stage1_baseline_full").resolve()
    output = (args.output or repo / "artifacts" / "problem3_g1_paired").resolve()
    cases = args.cases or [f"case_{i:03d}" for i in range(1, 101)]
    if len(set(cases)) != len(cases) or any(
            case not in {f"case_{i:03d}" for i in range(1, 101)}
            for case in cases):
        parser.error("--cases must be distinct case_001 through case_100")
    if len(set(args.cores)) != len(args.cores) or any(
            core not in range(1, 6) for core in args.cores):
        parser.error("--cores must be distinct integers in 1..5")

    ident = identity(repo)
    baseline_preflight(repo, baseline, ident)
    output.mkdir(parents=True, exist_ok=True)
    identity_path = output / "run_identity.json"
    if identity_path.exists() and load_json(identity_path) != ident:
        raise ValueError(f"G1 output identity changed: {identity_path}")
    if not identity_path.exists():
        write_json(identity_path, ident)
    config_path = repo / "data" / "config.txt"
    settings = read_evaluation_config(str(config_path))
    settings.update(read_scene_b_config(str(config_path)))
    settings.update(read_cache_config(str(config_path)))
    started = time.perf_counter()
    progress = []
    for case in cases:
        for cores in args.cores:
            try:
                record, resumed, calls = run_group(
                    repo, baseline, output, case, cores, ident, settings,
                    recheck_no_l2=args.recheck_no_l2,
                )
                progress.append({
                    "case": case, "cores": cores, "status": "success",
                    "disposition": "verified_resume" if resumed else "evaluated",
                    "official_calls": calls,
                    "elapsed_wall_seconds": record["elapsed_wall_seconds"],
                })
                print(f"{case}/k{cores} "
                      f"noL2={record['no_l2']['makespan']} "
                      f"L2={record['l2']['makespan']} "
                      f"{'resume' if resumed else 'evaluated'}", flush=True)
            except Exception as error:
                progress.append({
                    "case": case, "cores": cores, "status": "failed",
                    "error_type": type(error).__name__, "error": str(error),
                    "official_calls": None,
                })
                print(f"{case}/k{cores} FAILED: {type(error).__name__}: {error}",
                      file=sys.stderr, flush=True)
    summary = summarize(output)
    invocation = {
        "schema_version": 1,
        "git_head_at_finish": git_head(repo),
        "selected_cases": cases, "selected_cores": args.cores,
        "recheck_no_l2": args.recheck_no_l2,
        "progress": progress,
        "official_calls_this_invocation": sum(
            item["official_calls"] or 0 for item in progress),
        "verified_resume_count": sum(
            item["disposition"] == "verified_resume" for item in progress
            if item["status"] == "success"),
        "failed_count": sum(item["status"] == "failed" for item in progress),
        "elapsed_wall_seconds": time.perf_counter() - started,
        "summary": summary,
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    write_json(output / "invocations" / f"{timestamp}.json", invocation)
    return 1 if invocation["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
