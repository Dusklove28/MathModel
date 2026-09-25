"""Run the pre-registered Problem-2 Stage-3 Gate-A ordering diagnostic.

The design is intentionally fixed to three development case-core-family
groups.  Each group keeps the Stage-2 map_locality partition and mapping,
retains its original order, and generates at most two deterministic new
orders.  The runner never opens the 18-case blind validation set.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from candidate_manager_problem1 import sha256_file
from candidate_manager_problem2 import json_sha256
from candidate_manager_problem2_stage2 import (
    load_verified_stage1_selected_references,
)
from candidate_manager_problem2_stage3 import (
    ORDER_POLICIES,
    cache_key_summary,
    reusable_stage3_group,
    run_stage3_ordering_group,
    stage3_implementation_hash,
)


GATE_A_TARGETS: Tuple[Tuple[str, int, str], ...] = (
    ("case_064", 3, "b2a_w8"),
    ("case_067", 3, "b2a_w4"),
    ("case_058", 4, "b2a_w4"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for name in row:
            if name not in seen:
                seen.add(name)
                fieldnames.append(name)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _invocation_id(started_at: str) -> str:
    compact = started_at.replace("-", "").replace(":", "").replace("+", "p")
    compact = compact.replace(".", "p")
    return "{}-pid{}".format(compact, os.getpid())


def _append_history(output: Path, record: Mapping[str, Any]) -> None:
    path = output / "invocation_history.json"
    values: List[Dict[str, Any]] = []
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError("invocation_history.json must contain a list")
        values = [dict(item) for item in loaded]
    values.append(dict(record))
    _write_json(path, values)


def _candidate_rows(groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in groups:
        fixed = group["fixed_mapping_reference"]["metrics"]
        stage1 = group["stage1_global_reference"]["metrics"]
        for record in group["candidates"]:
            metrics = record.get("metrics") or {}
            rows.append({
                "case": group["case"],
                "cores": group["cores"],
                "partition_family": group["partition_family"],
                "candidate": record.get("name"),
                "policy": record.get("policy"),
                "status": record.get("status"),
                "legal": record.get("legal"),
                "deduplicated": record.get("deduplicated"),
                "canonical_candidate": record.get("canonical_candidate"),
                "cache_hit": record.get("cache_hit"),
                "evaluation_key": record.get("evaluation_key"),
                "plan_hash": record.get("plan_hash"),
                "makespan_cycles": metrics.get("makespan_cycles"),
                "added_copy_bytes": metrics.get("added_copy_bytes"),
                "spill_added_copy_bytes": metrics.get("spill_added_copy_bytes"),
                "cross_task_traffic_bytes": metrics.get(
                    "cross_task_traffic_bytes"),
                "cross_core_transfer_count": metrics.get(
                    "cross_core_transfer_count"),
                "delta_cycles_vs_fixed_mapping_original": (
                    None if not metrics else int(metrics["makespan_cycles"])
                    - int(fixed["makespan_cycles"])),
                "delta_added_copy_vs_fixed_mapping_original": (
                    None if not metrics else int(metrics["added_copy_bytes"])
                    - int(fixed["added_copy_bytes"])),
                "delta_spill_vs_fixed_mapping_original": (
                    None if not metrics else int(metrics["spill_added_copy_bytes"])
                    - int(fixed["spill_added_copy_bytes"])),
                "delta_cycles_vs_stage1_global_winner": (
                    None if not metrics else int(metrics["makespan_cycles"])
                    - int(stage1["makespan_cycles"])),
                "delta_added_copy_vs_stage1_global_winner": (
                    None if not metrics else int(metrics["added_copy_bytes"])
                    - int(stage1["added_copy_bytes"])),
                "delta_spill_vs_stage1_global_winner": (
                    None if not metrics else int(metrics["spill_added_copy_bytes"])
                    - int(stage1["spill_added_copy_bytes"])),
                "generation_seconds": (record.get("timing") or {}).get(
                    "generation_seconds"),
                "validation_seconds": (record.get("timing") or {}).get(
                    "validation_seconds"),
                "cache_lookup_seconds": (record.get("timing") or {}).get(
                    "cache_lookup_seconds"),
                "official_evaluation_seconds": (record.get("timing") or {}).get(
                    "official_evaluation_seconds"),
                "invocation_wall_seconds": (record.get("timing") or {}).get(
                    "invocation_wall_seconds"),
                "failure_stage": record.get("failure_stage"),
                "error_type": record.get("error_type"),
                "error": record.get("error"),
            })
    return rows


def _paired_rows(groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in groups:
        fixed = group["fixed_mapping_reference"]
        stage1 = group["stage1_global_reference"]
        best_new = group.get("best_new_ordering") or {}
        winner = group["winner_within_fixed_mapping"]
        final = group["final_case_core"]
        rows.append({
            "case": group["case"],
            "cores": group["cores"],
            "partition_family": group["partition_family"],
            "fixed_mapping_candidate": fixed["candidate"],
            "fixed_mapping_makespan_cycles": fixed["metrics"]["makespan_cycles"],
            "fixed_mapping_added_copy_bytes": fixed["metrics"]["added_copy_bytes"],
            "fixed_mapping_spill_added_copy_bytes": fixed["metrics"][
                "spill_added_copy_bytes"],
            "best_new_ordering": best_new.get("name"),
            "best_new_makespan_cycles": (best_new.get("metrics") or {}).get(
                "makespan_cycles"),
            "best_new_added_copy_bytes": (best_new.get("metrics") or {}).get(
                "added_copy_bytes"),
            "best_new_spill_added_copy_bytes": (best_new.get("metrics") or {}).get(
                "spill_added_copy_bytes"),
            "winner_within_fixed_mapping": winner["name"],
            "delta_cycles_vs_fixed_mapping_original": group[
                "delta_vs_fixed_mapping_original_order"]["makespan_cycles"],
            "delta_added_copy_vs_fixed_mapping_original": group[
                "delta_vs_fixed_mapping_original_order"]["added_copy_bytes"],
            "delta_spill_vs_fixed_mapping_original": group[
                "delta_vs_fixed_mapping_original_order"]["spill_added_copy_bytes"],
            "stage1_global_winner": stage1["winner"],
            "stage1_global_makespan_cycles": stage1["metrics"]["makespan_cycles"],
            "stage1_global_added_copy_bytes": stage1["metrics"]["added_copy_bytes"],
            "stage1_global_spill_added_copy_bytes": stage1["metrics"][
                "spill_added_copy_bytes"],
            "final_source": final["source"],
            "final_winner": final["winner"],
            "final_makespan_cycles": final["metrics"]["makespan_cycles"],
            "final_added_copy_bytes": final["metrics"]["added_copy_bytes"],
            "final_spill_added_copy_bytes": final["metrics"][
                "spill_added_copy_bytes"],
            "delta_cycles_vs_stage1_global_winner": final[
                "delta_vs_stage1_global_winner"]["makespan_cycles"],
            "delta_added_copy_vs_stage1_global_winner": final[
                "delta_vs_stage1_global_winner"]["added_copy_bytes"],
            "delta_spill_vs_stage1_global_winner": final[
                "delta_vs_stage1_global_winner"]["spill_added_copy_bytes"],
            "ordering_brought_final_case_core_improvement": final[
                "ordering_brought_final_case_core_improvement"],
        })
    return rows


def summarize(
    *,
    output: Path,
    groups: Sequence[Mapping[str, Any]],
    failures: Sequence[Mapping[str, Any]],
    run_identity: Mapping[str, Any],
    wall_seconds: float,
) -> Dict[str, Any]:
    candidate_rows = _candidate_rows(groups)
    paired_rows = _paired_rows(groups)
    _write_csv(output / "candidate_results.csv", candidate_rows)
    _write_csv(output / "paired_ordering_results.csv", paired_rows)
    _write_csv(output / "failures.csv", failures)
    cache_summary = cache_key_summary(groups)
    cache_summary["run_identity_sha256"] = run_identity[
        "run_identity_sha256"]
    cache_summary["timestamp"] = _utc_now()
    _write_json(output / "cache_key_summary.json", cache_summary)

    new_records = [row for row in candidate_rows
                   if str(row["candidate"]).startswith("order_")]
    final_improvements = sum(
        bool(row["ordering_brought_final_case_core_improvement"])
        for row in paired_rows)
    improvement_cases = sorted({
        str(row["case"]) for row in paired_rows
        if row["ordering_brought_final_case_core_improvement"]
    })
    technical_pass = (
        len(groups) == len(GATE_A_TARGETS)
        and not failures
        and len(new_records) == 2 * len(GATE_A_TARGETS)
        and all(bool(row["legal"]) for row in new_records)
        and sum(int(group["official_evaluations"]) for group in groups) <= 6
    )
    if final_improvements == 0:
        recommendation = "stop_ordering_expansion_and_prioritize_problem3"
    else:
        recommendation = (
            "freeze_ordering_rule_and_check_multi_case_distribution_on_"
            "the_12_case_development_set_before_any_blind_case")
    return {
        "schema_version": 1,
        "kind": "problem2_stage3_ordering_gate_a_summary",
        "complete": len(groups) == len(GATE_A_TARGETS) and not failures,
        "expected_groups": len(GATE_A_TARGETS),
        "recorded_groups": len(groups),
        "logical_new_ordering_candidates": len(new_records),
        "legal_new_ordering_candidates": sum(
            bool(row["legal"]) for row in new_records),
        "deduplicated_new_ordering_candidates": sum(
            bool(row["deduplicated"]) for row in new_records),
        "official_evaluations": sum(
            int(group["official_evaluations"]) for group in groups),
        "cache_hits": sum(int(group["cache_hits"]) for group in groups),
        "failed_candidates": sum(
            int(group["failed_ordering_candidates"]) for group in groups),
        "fixed_mapping_order_improvements": sum(
            int(row["delta_cycles_vs_fixed_mapping_original"] < 0)
            for row in paired_rows),
        "final_case_core_improvements_brought_by_ordering": final_improvements,
        "improvement_cases": improvement_cases,
        "improvement_case_count": len(improvement_cases),
        "technical_gate": {
            "passed": technical_pass,
            "criterion": (
                "3/3 frozen fixed-mapping groups complete; original order "
                "retained; six logical new orders legal; at most six unique "
                "new official evaluations; zero failures"),
        },
        "decision_rule": {
            "if_zero_final_case_core_improvements": (
                "stop ordering expansion and prioritize Problem 3"),
            "if_any_improvement": (
                "freeze the rule, then check whether gains span multiple "
                "development cases before considering 18 blind cases"),
            "recommendation": recommendation,
            "blind_cases_used": False,
        },
        "cache_key_summary": {
            key: cache_summary[key] for key in (
                "evaluation_key_references", "unique_evaluation_keys",
                "cache_hit_references", "evaluation_key_set_sha256")
        },
        "run_identity": dict(run_identity),
        "wall_seconds": wall_seconds,
        "timestamp": _utc_now(),
        "failures": list(failures),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument(
        "--baseline", type=Path,
        default=Path("artifacts/problem2_stage1_baseline_cold_audit"))
    parser.add_argument(
        "--stage2", type=Path,
        default=Path("artifacts/problem2_stage2_mapping_stratified12_locality"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()

    started_at = _utc_now()
    invocation_id = _invocation_id(started_at)
    repo = args.repo.resolve()
    baseline = (repo / args.baseline).resolve() if not args.baseline.is_absolute() else args.baseline.resolve()
    stage2 = (repo / args.stage2).resolve() if not args.stage2.is_absolute() else args.stage2.resolve()
    output = (repo / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    cache = (repo / args.cache).resolve() if not args.cache.is_absolute() else args.cache.resolve()
    config_path = repo / "data" / "config.txt"
    if not config_path.is_file():
        parser.error("repository/data/config.txt is missing")
    if not baseline.is_dir():
        parser.error("verified Stage-1 baseline directory is missing")
    if not stage2.is_dir():
        parser.error("frozen Stage-2 stratified12 directory is missing")

    pairs = tuple((case, cores) for case, cores, _ in GATE_A_TARGETS)
    try:
        selected = load_verified_stage1_selected_references(
            graph_root=repo / "data",
            config_path=config_path,
            baseline_root=baseline,
            pairs=pairs,
        )
    except Exception as error:
        parser.error("Stage-1 global fallback validation failed: {}".format(error))
    implementation_hash, implementation_files = stage3_implementation_hash()
    run_identity = {
        "implementation_sha256": implementation_hash,
        "implementation_file_sha256": implementation_files,
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "preset": "gate-a",
        "targets": [list(item) for item in GATE_A_TARGETS],
        "ordering_policies": list(ORDER_POLICIES),
        "maximum_unique_new_official_evaluations": 6,
        "baseline": str(baseline),
        "stage2": str(stage2),
        "stage2_run_identity_file_sha256": sha256_file(
            stage2 / "run_identity.json"),
        "stage1_selected_results_file_sha256": (
            selected.selected_results_file_hash),
        "stage1_selected_reference_sha256": (
            selected.selected_reference_hash),
        "cache": str(cache),
        "blind_cases_used": False,
    }
    run_identity["run_identity_sha256"] = json_sha256(run_identity)
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "run_identity.json", run_identity)

    started = time.perf_counter()
    groups: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for case, cores, family in GATE_A_TARGETS:
        graph_path = repo / "data" / (case + ".json")
        group_path = output / "groups" / case / "k{}".format(cores) / (
            family + ".json")
        reusable = reusable_stage3_group(
            group_path=group_path,
            graph_path=graph_path,
            config_path=config_path,
            baseline_root=baseline,
            stage2_root=stage2,
            cores=cores,
            family=family,
        )
        if reusable is not None:
            reusable["_resume"] = True
            groups.append(reusable)
            print("{} k{} {}: resumed".format(case, cores, family), flush=True)
        else:
            try:
                result = run_stage3_ordering_group(
                    graph_path=graph_path,
                    config_path=config_path,
                    baseline_root=baseline,
                    stage2_root=stage2,
                    output_root=output,
                    cache_dir=cache,
                    cores=cores,
                    family=family,
                )
                group = result.manifest
                group["_resume"] = False
                groups.append(group)
                print("{} k{} {}: success [{}]".format(
                    case, cores, family,
                    group["winner_within_fixed_mapping"]["name"]), flush=True)
            except Exception as error:
                failure = {
                    "case": case,
                    "cores": cores,
                    "partition_family": family,
                    "error_type": type(error).__name__,
                    "error": "{}: {}".format(type(error).__name__, error),
                }
                failures.append(failure)
                print("{} k{} {}: failed {}".format(
                    case, cores, family, failure["error"]), flush=True)
        _write_json(output / "progress.json", {
            "expected_groups": len(GATE_A_TARGETS),
            "completed_groups": len(groups),
            "failures": failures,
            "elapsed_seconds": time.perf_counter() - started,
            "timestamp": _utc_now(),
        })

    wall_seconds = time.perf_counter() - started
    summary = summarize(
        output=output,
        groups=groups,
        failures=failures,
        run_identity=run_identity,
        wall_seconds=wall_seconds,
    )
    summary["latest_invocation_id"] = invocation_id
    _write_json(output / "summary.json", summary)
    invocation = {
        "invocation_id": invocation_id,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "run_identity_sha256": run_identity["run_identity_sha256"],
        "expected_groups": len(GATE_A_TARGETS),
        "recorded_groups": len(groups),
        "executed_groups": sum(not bool(group.get("_resume")) for group in groups),
        "resumed_groups": sum(bool(group.get("_resume")) for group in groups),
        "invocation_official_evaluations": sum(
            int(group["official_evaluations"])
            for group in groups if not group.get("_resume")),
        "design_official_evaluations": summary["official_evaluations"],
        "failed_groups": len(failures),
        "complete": summary["complete"],
        "technical_gate_passed": summary["technical_gate"]["passed"],
        "recommendation": summary["decision_rule"]["recommendation"],
        "wall_seconds": wall_seconds,
    }
    _write_json(output / "invocations" / invocation_id / "summary.json", invocation)
    _append_history(output, invocation)
    print(json.dumps({
        "complete": summary["complete"],
        "groups": summary["recorded_groups"],
        "official_evaluations": summary["official_evaluations"],
        "technical_gate_passed": summary["technical_gate"]["passed"],
        "final_case_core_improvements_brought_by_ordering": summary[
            "final_case_core_improvements_brought_by_ordering"],
        "recommendation": summary["decision_rule"]["recommendation"],
        "blind_cases_used": False,
    }, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if summary["technical_gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

