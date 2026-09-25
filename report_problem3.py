"""Build 500-row paired curves and 2x2 official-evaluator ablation evidence."""

from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
import sys
import time
from pathlib import Path

from audit_problem3_assets import sha256_file
from run_problem3_g1 import (
    REPO, compact_metrics, json_sha256, load_json, write_csv, write_json,
)

sys.path.insert(0, str(REPO / "code"))
from evaluation_validation import read_evaluation_config
from multicore_cut_evaluate_problem_2 import evaluate_scene_b, read_scene_b_config


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    return (ordered[lower] * (upper - position)
            + ordered[upper] * (position - lower)
            if lower != upper else ordered[lower])


def _bootstrap_mean_interval(values: list[float], *, seed: int) -> list[float]:
    rng = random.Random(seed)
    n = len(values)
    draws = [statistics.mean(values[rng.randrange(n)] for _ in range(n))
             for _ in range(2000)]
    return [_percentile(draws, 0.025), _percentile(draws, 0.975)]


def best_observed_one_core(stage1: Path, repo: Path, g1_identity: dict) -> dict:
    """Verify the fastest archived candidate from the requested-k1 groups."""
    archived = load_json(stage1 / "run_identity.json")
    if archived["config_sha256"] != g1_identity["config_sha256"]:
        raise ValueError("Stage-1 configuration differs from G1")
    for name, digest in archived["evaluator_file_sha256"].items():
        if g1_identity["official_file_lf_sha256"].get(name) != digest:
            raise ValueError(f"Stage-1 official dependency differs: {name}")
    with (stage1 / "candidate_results.csv").open(
            "r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    selected = {}
    for row in rows:
        if (row["cores"] != "1" or row["status"] != "evaluated"
                or row["legal"] != "True"
                or row["used_core_count"] != "1"):
            continue
        case = row["case"]
        key = (int(row["makespan_cycles"]), int(row["added_copy_bytes"]),
               int(row["cores"]), row["candidate"])
        if case not in selected or key < selected[case][0]:
            selected[case] = (key, row)
    if len(selected) != 100:
        raise ValueError(f"only {len(selected)} cases have verified one-core candidates")
    verified = {}
    for case, (key, row) in selected.items():
        cores, candidate = int(row["cores"]), row["candidate"]
        if Path(candidate).name != candidate:
            raise ValueError(f"invalid archived candidate name: {candidate}")
        group = stage1 / "groups" / case / f"k{cores}"
        record = load_json(group / "candidate_records" / f"{candidate}.json")
        item = record["candidate"]
        plan = group / "plans" / Path(item["plan_path"]).name
        official = group / "official_results" / Path(
            item["official_result_path"]).name
        if (record["graph_sha256"] != sha256_file(
                repo / "data" / f"{case}.json")
                or record["config_sha256"] != archived["config_sha256"]
                or record["evaluator_sha256"] != archived["evaluator_sha256"]
                or item["name"] != candidate or item["status"] != "evaluated"
                or item["legal"] is not True
                or item["plan_file_sha256"] != row["plan_file_sha256"]
                or sha256_file(plan) != item["plan_file_sha256"]
                or sha256_file(official) != item["official_result_file_sha256"]):
            raise ValueError(f"archived one-core candidate hash mismatch: {case}")
        result = load_json(official)
        plan_json = load_json(plan)
        if (json_sha256(result) != item["official_result_json_sha256"]
                or result["makespan"] != key[0]
                or item["metrics"]["used_core_count"] != 1
                or sum(bool(schedule) for schedule in
                       plan_json["core_schedules"]) != 1):
            raise ValueError(f"archived one-core candidate metric mismatch: {case}")
        verified[case] = {
            "makespan": key[0], "source_requested_cores": cores,
            "candidate": candidate, "plan_file_sha256": sha256_file(plan),
            "official_result_file_sha256": sha256_file(official),
        }
    return verified


def final_no_l2(
    *, repo: Path, output: Path, case: str, cores: int,
    graph: dict, plan: dict, plan_file_hash: str,
    final_l2: dict, settings: dict, p2_identity: dict,
) -> tuple[dict, bool]:
    path = output / "final_no_l2" / case / f"k{cores}.json"
    manifest_path = path.with_name(f"k{cores}_identity.json")
    fingerprint = json_sha256({
        "graph_sha256": sha256_file(repo / "data" / f"{case}.json"),
        "plan_file_sha256": plan_file_hash,
        "p2_identity": p2_identity,
    })
    if path.exists() or manifest_path.exists():
        if not path.exists() or not manifest_path.exists():
            raise ValueError(f"partial final no-L2 ablation: {case}/k{cores}")
        manifest = load_json(manifest_path)
        if manifest.get("fingerprint") != fingerprint or sha256_file(path) != manifest.get(
                "result_file_sha256"):
            raise ValueError(f"final no-L2 ablation identity mismatch: {case}/k{cores}")
        result = load_json(path)
        if json_sha256(result) != manifest.get("result_json_sha256"):
            raise ValueError(f"final no-L2 JSON hash mismatch: {case}/k{cores}")
        return result, False
    result = evaluate_scene_b(
        graph, plan, settings["bandwidth"], settings["capacity"],
        settings["cross_core_copy_delay_cycles"],
    )
    if result["num_cores"] != cores:
        raise ValueError(f"final no-L2 core mismatch: {case}/k{cores}")
    if result["data_movement_bytes"] != final_l2["data_movement_bytes"]:
        raise ValueError(f"final no-L2/L2 movement mismatch: {case}/k{cores}")
    write_json(path, result)
    write_json(manifest_path, {
        "fingerprint": fingerprint,
        "result_file_sha256": sha256_file(path),
        "result_json_sha256": json_sha256(result),
    })
    return result, True


def verify_final_source(repo: Path, g1: Path, g2: Path,
                        source_g2: Path | None) -> None:
    recorded = load_json(g2 / "run_identity.json")
    cases = [f"case_{i:03d}" for i in range(1, 101)]
    if recorded.get("experiment") == "problem3_g2_reuse_window_v1":
        from run_problem3_g2 import completed_records, identity
        if recorded != identity(repo, g1):
            raise ValueError("G2 ordering identity differs from current files")
        completed_records(repo, g1, g2, recorded, cases)
    elif recorded.get("experiment") == "problem3_shared_input_one_move_probe_v1":
        from probe_problem3_mapping import completed_records, identity
        if source_g2 is None:
            raise ValueError("mapping report requires --g2-source")
        if recorded != identity(repo, source_g2, g1):
            raise ValueError("G2 mapping identity differs from current files")
        completed_records(repo, g1, source_g2, g2, recorded, cases)
    else:
        raise ValueError("unknown G2 final source identity")


def build(repo: Path, g1: Path, g2: Path | None, output: Path,
          mode: str, source_g2: Path | None = None,
          stage1: Path | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    config_path = repo / "data" / "config.txt"
    settings = read_evaluation_config(str(config_path))
    settings.update(read_scene_b_config(str(config_path)))
    g1_identity = load_json(g1 / "run_identity.json")
    if sha256_file(config_path, normalize_newlines=True) != g1_identity["config_sha256"]:
        raise ValueError("current configuration differs from G1")
    for name, expected in g1_identity["official_file_lf_sha256"].items():
        if sha256_file(repo / "code" / name, normalize_newlines=True) != expected:
            raise ValueError(f"current official dependency differs from G1: {name}")
    p2_identity = {
        "config_lf_sha256": sha256_file(config_path, normalize_newlines=True),
        "official_file_lf_sha256": {
            name: digest for name, digest in
            g1_identity["official_file_lf_sha256"].items()
            if name != "multicore_cut_evaluate_problem_3.py"
        },
    }
    if mode == "g2":
        if g2 is None:
            raise ValueError("--g2 is required in g2 mode")
        verify_final_source(repo, g1, g2, source_g2)
    stage1 = stage1 or repo / "artifacts" / "problem2_stage1_baseline_full"
    one_core_candidates = best_observed_one_core(stage1, repo, g1_identity)
    records = []
    official_calls = 0
    started = time.perf_counter()
    for i in range(1, 101):
        case = f"case_{i:03d}"
        graph_path = repo / "data" / f"{case}.json"
        graph = None
        for cores in range(1, 6):
            group_dir = g1 / "groups" / case / f"k{cores}"
            g1_record = load_json(group_dir / "group.json")
            if g1_record.get("status") != "success":
                raise ValueError(f"G1 group failed: {case}/k{cores}")
            baseline_plan_path = group_dir / "winner_plan.json"
            baseline_p2_path = group_dir / "no_l2_official.json"
            baseline_p3_path = group_dir / "l2_official.json"
            for path, key in (
                (baseline_plan_path, "plan_file_sha256"),
                (baseline_p2_path, "no_l2_result_file_sha256"),
                (baseline_p3_path, "l2_result_file_sha256"),
            ):
                if sha256_file(path) != g1_record[key]:
                    raise ValueError(f"G1 group source SHA256 mismatch: {path}")
            baseline_plan = load_json(baseline_plan_path)
            baseline_p2 = load_json(baseline_p2_path)
            baseline_p3 = load_json(baseline_p3_path)
            final_plan, final_p3 = baseline_plan, baseline_p3
            final_plan_path = baseline_plan_path
            final_source = "G1_baseline"
            g2_record = None
            if mode == "g2" and cores >= 2:
                if g2 is None:
                    raise ValueError("--g2 is required in g2 mode")
                g2_dir = g2 / "groups" / case / f"k{cores}"
                g2_record = load_json(g2_dir / "group.json")
                if g2_record.get("status") != "success":
                    raise ValueError(f"G2 group failed: {case}/k{cores}")
                final_plan_path = g2_dir / "final_multicore_res.json"
                final_p3_path = g2_dir / "final_official_evaluation.json"
                for path in (final_plan_path, final_p3_path):
                    relative = str(path.relative_to(g2_dir)).replace("\\", "/")
                    if sha256_file(path) != g2_record["file_sha256"][relative]:
                        raise ValueError(f"G2 winner SHA256 mismatch: {path}")
                final_plan, final_p3 = load_json(final_plan_path), load_json(final_p3_path)
                final_source = g2_record["winner"]
            plan_changed = json_sha256(final_plan) != json_sha256(baseline_plan)
            if not plan_changed and json_sha256(final_p3) != json_sha256(baseline_p3):
                raise ValueError(f"same plan produced different L2 result: {case}/k{cores}")
            if plan_changed:
                if graph is None:
                    graph = load_json(graph_path)
                final_p2, called = final_no_l2(
                    repo=repo, output=output, case=case, cores=cores,
                    graph=graph, plan=final_plan,
                    plan_file_hash=sha256_file(final_plan_path),
                    final_l2=final_p3, settings=settings,
                    p2_identity=p2_identity,
                )
                official_calls += called
            else:
                final_p2 = baseline_p2
            p2_base = compact_metrics(baseline_p2, l2=False)
            p3_base = compact_metrics(baseline_p3, l2=True)
            p2_final = compact_metrics(final_p2, l2=False)
            p3_final = compact_metrics(final_p3, l2=True)
            row = {
                "case": case, "cores": cores,
                "graph_sha256": g1_record["graph_sha256"],
                "baseline_plan_file_sha256": g1_record["plan_file_sha256"],
                "final_plan_file_sha256": sha256_file(final_plan_path),
                "final_source": final_source,
                "plan_changed": plan_changed,
                "baseline_legal": True, "final_legal": True,
                "g1_no_l2_official_calls": g1_record["official_calls"]["problem_2"],
                "g1_l2_official_calls": g1_record["official_calls"]["problem_3"],
                "g2_official_calls": (g2_record["official_calls"]
                                      if g2_record is not None else 0),
                "g2_candidate_cache_hits": (g2_record["candidate_cache_hits"]
                                            if g2_record is not None else 0),
                "final_no_l2_source": ("new_official_evaluation" if plan_changed
                                       else "G1_verified_reuse"),
                "best_observed_one_core_no_l2_makespan":
                    one_core_candidates[case]["makespan"],
                "best_observed_one_core_source_requested_cores":
                    one_core_candidates[case]["source_requested_cores"],
                "best_observed_one_core_candidate":
                    one_core_candidates[case]["candidate"],
                "best_observed_one_core_plan_file_sha256":
                    one_core_candidates[case]["plan_file_sha256"],
                "best_observed_one_core_result_file_sha256":
                    one_core_candidates[case]["official_result_file_sha256"],
            }
            for name, values in (
                ("baseline_no_l2", p2_base), ("baseline_l2", p3_base),
                ("final_no_l2", p2_final), ("final_l2", p3_final),
            ):
                row.update({f"{name}_{key}": value for key, value in values.items()})
            row.update({
                "hardware_speedup_baseline_mean_of_ratios_member":
                    p2_base["makespan"] / p3_base["makespan"],
                "hardware_speedup_final_mean_of_ratios_member":
                    p2_final["makespan"] / p3_final["makespan"],
                "l2_plan_tuning_speedup_mean_of_ratios_member":
                    p3_base["makespan"] / p3_final["makespan"],
                "no_l2_plan_tuning_speedup_mean_of_ratios_member":
                    p2_base["makespan"] / p2_final["makespan"],
                "hardware_plan_interaction_cycles":
                    (p2_final["makespan"] - p3_final["makespan"])
                    - (p2_base["makespan"] - p3_base["makespan"]),
            })
            records.append(row)
    if len(records) != 500:
        raise ValueError(f"ablation has {len(records)} rows, expected 500")
    one_core_by_case = {row["case"]: row for row in records if row["cores"] == 1}
    for row in records:
        reference = one_core_by_case[row["case"]]
        for label in ("baseline_no_l2", "baseline_l2",
                      "final_no_l2", "final_l2"):
            row[f"{label}_parallel_speedup_vs_same_case_k1"] = (
                reference[f"{label}_makespan"] / row[f"{label}_makespan"])
        for label in ("baseline_no_l2", "final_no_l2"):
            row[f"{label}_parallel_speedup_vs_best_observed_one_core"] = (
                row["best_observed_one_core_no_l2_makespan"] /
                row[f"{label}_makespan"])
    write_csv(output / "case_core_2x2.csv", records, list(records[0]))
    curves, summary = [], []
    for cores in range(1, 6):
        subset = [row for row in records if row["cores"] == cores]
        curve = {"cores": cores, "cases": len(subset)}
        for label in ("baseline_no_l2", "baseline_l2",
                      "final_no_l2", "final_l2"):
            values = [row[f"{label}_makespan"] for row in subset]
            curve[f"mean_{label}_makespan"] = statistics.mean(values)
            curve[f"median_{label}_makespan"] = statistics.median(values)
            curve[f"mean_of_case_parallel_speedups_{label}_vs_k1"] = statistics.mean(
                row[f"{label}_parallel_speedup_vs_same_case_k1"]
                for row in subset)
            curve[f"ratio_of_mean_parallel_speedups_{label}_vs_k1"] = (
                statistics.mean(one_core_by_case[row["case"]][f"{label}_makespan"]
                                for row in subset) / statistics.mean(values))
        for label in ("baseline_no_l2", "final_no_l2"):
            curve[f"mean_of_case_parallel_speedups_{label}_vs_best_observed_one_core"] = (
                statistics.mean(row[
                    f"{label}_parallel_speedup_vs_best_observed_one_core"]
                    for row in subset))
        for label in (
            "hardware_speedup_baseline_mean_of_ratios_member",
            "hardware_speedup_final_mean_of_ratios_member",
            "l2_plan_tuning_speedup_mean_of_ratios_member",
            "no_l2_plan_tuning_speedup_mean_of_ratios_member",
        ):
            values = [row[label] for row in subset]
            short = label.removesuffix("_mean_of_ratios_member")
            curve[f"mean_of_case_ratios_{short}"] = statistics.mean(values)
            curve[f"median_of_case_ratios_{short}"] = statistics.median(values)
            curve[f"wins_{short}"] = sum(value > 1 for value in values)
            curve[f"worst_{short}"] = min(values)
            curve[f"bootstrap_95ci_mean_{short}"] = _bootstrap_mean_interval(
                values, seed=20260925 + cores + len(short),
            )
        curve["ratio_of_mean_makespans_hardware_baseline"] = (
            curve["mean_baseline_no_l2_makespan"] /
            curve["mean_baseline_l2_makespan"])
        curve["ratio_of_mean_makespans_l2_tuning"] = (
            curve["mean_baseline_l2_makespan"] /
            curve["mean_final_l2_makespan"])
        curves.append(curve)
        summary.append({"cores": cores, **curve})
    flat_curves = [{key: value for key, value in row.items()
                    if not isinstance(value, list)} for row in curves]
    write_csv(output / "curve_1_to_5.csv", flat_curves, list(flat_curves[0]))
    report = {
        "schema_version": 1, "mode": mode,
        "result_rows": len(records),
        "case_count": 100,
        "config_lf_sha256": p2_identity["config_lf_sha256"],
        "official_file_lf_sha256": g1_identity["official_file_lf_sha256"],
        "g1_run_identity_file_sha256": sha256_file(g1 / "run_identity.json"),
        "stage1_run_identity_file_sha256": sha256_file(stage1 / "run_identity.json"),
        "stage1_candidate_results_file_sha256": sha256_file(
            stage1 / "candidate_results.csv"),
        "g2_run_identity_file_sha256": (
            sha256_file(g2 / "run_identity.json")
            if mode == "g2" and g2 is not None else None),
        "report_code_sha256": sha256_file(Path(__file__)),
        "official_problem2_calls_this_invocation": official_calls,
        "elapsed_wall_seconds": time.perf_counter() - started,
        "metric_definition": {
            "same_core_hardware_speedup": "T_noL2/T_L2 per case-core",
            "aggregate_speedup": "arithmetic mean of 100 per-case ratios, with percentile bootstrap by case; ratio of mean makespans separately shown",
            "plan_tuning_speedup": "T_baseline_plan,L2/T_final_plan,L2 per case-core",
            "interaction_cycles": "(T_final,noL2-T_final,L2)-(T_baseline,noL2-T_baseline,L2)",
            "cache_hit_rate": "official byte-weighted COPY_IN hit rate, not scoring objective",
            "parallel_speedup_denominator_frozen_k1": "frozen Problem-2 Stage-1 winner at requested k=1 for the same case",
            "parallel_speedup_denominator_best_observed_one_core_no_l2": "fastest verified official Problem-2 Stage-1 candidate among requested-k1 groups; applies only to no-L2 curves",
        },
        "curves": curves,
    }
    write_json(output / "summary.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--g1", type=Path)
    parser.add_argument("--g2", type=Path)
    parser.add_argument("--g2-source", type=Path,
                        help="ordering output that a mapping final output reads")
    parser.add_argument("--stage1", type=Path,
                        help="archived Problem-2 Stage-1 candidate directory")
    parser.add_argument("--mode", choices=("g1", "g2"), default="g1")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    g1 = (args.g1 or repo / "artifacts" / "problem3_g1_paired").resolve()
    g2 = args.g2.resolve() if args.g2 else repo / "artifacts" / "problem3_g2_ordering"
    output = (args.output or repo / "artifacts" /
              ("problem3_report_g1" if args.mode == "g1"
               else "problem3_report_g2")).resolve()
    source_g2 = args.g2_source.resolve() if args.g2_source else None
    stage1 = args.stage1.resolve() if args.stage1 else None
    report = build(repo, g1, g2, output, args.mode, source_g2, stage1)
    print(f"mode={report['mode']} rows={report['result_rows']} "
          f"official_p2_calls={report['official_problem2_calls_this_invocation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
