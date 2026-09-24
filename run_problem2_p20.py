"""Cross-evaluate the six frozen Problem-1 methods with the official Problem-2 evaluator.

This experiment changes no partitioning or scheduling algorithm. Existing
candidate plans are copied when present; missing ones are regenerated with the
current Problem-1 solver. Frozen signatures are included in the handoff when
the original Problem-1 artifacts are unavailable. Use a separate output path.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


METHODS = ("single", "b0", "b1", "b2a_w4", "b2a_w8", "b2a_w16")
DEFAULT_CASES = ("case_001", "case_050", "case_100")
DEFAULT_CORES = (2, 4, 5)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def load_repo_modules(repo: Path) -> dict[str, Any]:
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "code"))
    from contest_io import _read_json
    from candidate_manager_problem1 import canonical_plan_signature
    from evaluation_validation import read_evaluation_config
    from multicore_cut_evaluate_problem_2 import evaluate_scene_b, read_scene_b_config
    from solver_problem1 import (
        GraphInfo,
        generate_problem1_plan_from_graph_info,
        generate_single_plan_from_graph_info,
    )
    from stub_multicore_cut_and_schedule import derive_multicore_plan

    return locals()


def source_identity(repo: Path) -> dict[str, Any]:
    files = [repo / "solver_problem1.py", repo / "candidate_manager_problem1.py"]
    files.extend(sorted((repo / "code").rglob("*.py")))
    return {
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip(),
        "file_sha256": {
            path.relative_to(repo).as_posix(): sha256(path) for path in files
        },
    }


def candidate_source(repo: Path, case: str, cores: int, method: str) -> Path:
    return (
        repo / "artifacts" / "problem1_candidates" / case / f"k{cores}"
        / "candidates" / f"{method}_multicore_res.json"
    )


def frozen_group(repo: Path, case: str, cores: int) -> tuple[Path, dict[str, Any]]:
    path = (
        repo / "artifacts" / "problem1_full_c4140" / "groups"
        / case / f"k{cores}.json"
    )
    if not path.is_file():
        path = (
            repo / "handoff" / "problem2_p20" / "frozen_groups"
            / case / f"k{cores}.json"
        )
    return path, json.loads(path.read_text(encoding="utf-8"))


def build_or_copy_plan(
    repo: Path, output: Path, case: str, cores: int, method: str,
    graph: dict[str, Any], modules: dict[str, Any], features: Any,
) -> tuple[dict[str, Any], Path, str]:
    original = candidate_source(repo, case, cores, method)
    if original.is_file():
        plan = modules["_read_json"](original)
        provenance = "existing_problem1_candidate"
    elif method == "single":
        plan = modules["generate_single_plan_from_graph_info"](features, cores)
        provenance = "regenerated_from_unchanged_problem1_solver"
    else:
        method_name = "B2A" if method.startswith("b2a_w") else method.upper()
        windows = int(method.rsplit("w", 1)[1]) if method_name == "B2A" else None
        plan, _ = modules["generate_problem1_plan_from_graph_info"](
            features, cores, method=method_name, windows=windows
        )
        provenance = "regenerated_from_unchanged_problem1_solver"
    modules["derive_multicore_plan"](graph, plan)
    path = output / "plans" / case / f"k{cores}" / f"{method}_multicore_res.json"
    write_json(path, plan)
    return plan, path, provenance


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    movement = result["data_movement_bytes"]
    added = movement["added_copy_bytes"]
    scheduled = movement["scheduled_copy_bytes"]
    spill = movement["spill_added_copy_bytes"]
    return {
        "makespan_cycles": result["makespan"],
        "original_graph_copy_bytes": movement["original_graph_copy_bytes"],
        "scheduled_copy_bytes": scheduled,
        "added_copy_bytes": added,
        "partition_added_copy_bytes": movement["partition_added_copy_bytes"],
        "spill_added_copy_bytes": spill,
        "spill_share_of_added": spill / added if added else 0.0,
        "spill_share_of_scheduled": spill / scheduled if scheduled else 0.0,
        "cross_task_traffic_bytes": result["cross_task_traffic"],
        "cross_core_transfer_count": len(result["cross_core_transfers"]),
        "used_core_count": sum(
            bool(item["ops"]) for item in result["per_core_timeline"]
        ),
        "memory_peak_by_core": result["memory_peak_by_core"],
    }


def summarize(output: Path, cases: tuple[str, ...], cores: tuple[int, ...]) -> int:
    records = []
    for case in cases:
        for core_count in cores:
            for method in METHODS:
                path = output / "records" / case / f"k{core_count}" / f"{method}.json"
                if path.is_file():
                    records.append(json.loads(path.read_text(encoding="utf-8")))
    fields = (
        "case", "cores", "method", "status", "makespan_cycles",
        "added_copy_bytes", "partition_added_copy_bytes", "spill_added_copy_bytes",
        "spill_share_of_added", "spill_share_of_scheduled",
        "cross_task_traffic_bytes", "cross_core_transfer_count",
        "used_core_count", "runtime_seconds", "plan_provenance",
        "frozen_signature_match", "error",
    )
    output.mkdir(parents=True, exist_ok=True)
    with (output / "p20_results.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field) for field in fields})
    winners = []
    for case in cases:
        for core_count in cores:
            group = [
                record for record in records
                if record["case"] == case and record["cores"] == core_count
                and record["status"] == "success"
            ]
            if group:
                best = min(group, key=lambda item: (
                    item["makespan_cycles"], item["added_copy_bytes"],
                    METHODS.index(item["method"]),
                ))
                winners.append({
                    "case": case, "cores": core_count, "method": best["method"],
                    "makespan_cycles": best["makespan_cycles"],
                    "added_copy_bytes": best["added_copy_bytes"],
                    "spill_added_copy_bytes": best["spill_added_copy_bytes"],
                })
    failure_count = sum(item["status"] != "success" for item in records)
    write_json(output / "p20_summary.json", {
        "expected_evaluations": len(cases) * len(cores) * len(METHODS),
        "record_count": len(records),
        "success_count": sum(item["status"] == "success" for item in records),
        "failure_count": failure_count,
        "winners": winners,
    })
    return failure_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", default=DEFAULT_CASES)
    parser.add_argument("--cores", nargs="+", type=int, default=DEFAULT_CORES)
    parser.add_argument("--limit", type=int, help="Maximum new evaluations for a smoke run")
    args = parser.parse_args()
    repo, output = args.repo.resolve(), args.output.resolve()
    cases, core_counts = tuple(args.cases), tuple(args.cores)
    modules = load_repo_modules(repo)
    config_path = repo / "data" / "config.txt"
    settings = modules["read_evaluation_config"](str(config_path))
    scene = modules["read_scene_b_config"](str(config_path))
    identity = source_identity(repo)
    identity["config_sha256"] = sha256(config_path)
    identity["runner_sha256"] = sha256(Path(__file__))
    write_json(output / "p20_identity.json", identity)

    new_evaluations = 0
    for case in cases:
        graph_path = repo / "data" / f"{case}.json"
        graph = modules["_read_json"](graph_path)
        features = None
        for cores in core_counts:
            group_path, group = frozen_group(repo, case, cores)
            expected_signatures = {
                item["name"]: item["canonical_signature"]
                for item in group["candidates"]
            }
            for method in METHODS:
                record_path = output / "records" / case / f"k{cores}" / f"{method}.json"
                source_path = candidate_source(repo, case, cores, method)
                source_hash = sha256(source_path) if source_path.is_file() else None
                plan_path = output / "plans" / case / f"k{cores}" / f"{method}_multicore_res.json"
                if record_path.is_file():
                    previous = json.loads(record_path.read_text(encoding="utf-8"))
                    if (previous.get("status") == "success"
                            and previous.get("graph_sha256") == sha256(graph_path)
                            and previous.get("config_sha256") == identity["config_sha256"]
                            and previous.get("source_identity") == identity
                            and previous.get("candidate_source_sha256") == source_hash
                            and previous.get("frozen_group_sha256") == sha256(group_path)
                            and previous.get("plan_sha256") == (
                                sha256(plan_path) if plan_path.is_file() else None
                            )):
                        continue
                if args.limit is not None and new_evaluations >= args.limit:
                    return 1 if summarize(output, cases, core_counts) else 0
                started = time.perf_counter()
                record: dict[str, Any] = {
                    "case": case, "cores": cores, "method": method,
                    "graph_sha256": sha256(graph_path),
                    "config_sha256": identity["config_sha256"],
                    "source_identity": identity,
                    "candidate_source_path": str(source_path) if source_hash else None,
                    "candidate_source_sha256": source_hash,
                    "frozen_group_path": str(group_path),
                    "frozen_group_sha256": sha256(group_path),
                    "frozen_signature_expected": expected_signatures[method],
                }
                try:
                    if features is None:
                        features = modules["GraphInfo"].from_graph(graph)
                    plan, plan_path, provenance = build_or_copy_plan(
                        repo, output, case, cores, method, graph, modules, features
                    )
                    record.update({
                        "plan_path": str(plan_path),
                        "plan_sha256": sha256(plan_path),
                        "plan_provenance": provenance,
                        "subgraph_count": len(set(plan["node_to_subgraph"].values())),
                    })
                    actual_signature = modules["canonical_plan_signature"](plan)
                    record["frozen_signature_actual"] = actual_signature
                    record["frozen_signature_match"] = (
                        actual_signature == expected_signatures[method]
                    )
                    if not record["frozen_signature_match"]:
                        raise ValueError("plan differs from frozen Problem-1 candidate")
                    result = modules["evaluate_scene_b"](
                        graph, plan,
                        bandwidth=settings["bandwidth"],
                        capacity=settings["capacity"],
                        cross_core_copy_delay=scene["cross_core_copy_delay_cycles"],
                    )
                    record.update(compact_result(result))
                    record["status"] = "success"
                    record["error"] = None
                except Exception as error:
                    record["status"] = "failed"
                    record["error"] = f"{type(error).__name__}: {error}"
                record["runtime_seconds"] = time.perf_counter() - started
                write_json(record_path, record)
                new_evaluations += 1
                print(
                    f"{case} k{cores} {method}: {record['status']} "
                    f"makespan={record.get('makespan_cycles')} "
                    f"spill={record.get('spill_added_copy_bytes')} "
                    f"time={record['runtime_seconds']:.3f}s",
                    flush=True,
                )
                summarize(output, cases, core_counts)
    return 1 if summarize(output, cases, core_counts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
