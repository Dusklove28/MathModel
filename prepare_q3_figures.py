"""Prepare auditable Problem 3 figure and table data from frozen inputs."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from validate_q3_figures import ROOT, REPORT, main as validate

RESULTS = ROOT / "results"
TIMELINE = ROOT / "artifacts/problem3_timeline_evidence"


def load(path):
    with Path(path).open(encoding="utf-8") as f:
        return json.load(f)


def write_csv(name, rows):
    assert rows
    RESULTS.mkdir(exist_ok=True)
    with (RESULTS / name).open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    validate()
    with (REPORT / "case_core_2x2.csv").open(encoding="utf-8-sig", newline="") as f:
        pairs = list(csv.DictReader(f))
    with (REPORT / "curve_1_to_5.csv").open(encoding="utf-8-sig", newline="") as f:
        curves = list(csv.DictReader(f))
    split = load(ROOT / "problem3_preregistered_split.json")
    phase_by_case = {c: phase for phase in ("development", "validation", "holdout") for c in split[f"{phase}_cases"]}

    raw_cases, raw_tensors = [], []
    for idx in range(1, 101):
        case = f"case_{idx:03d}"
        path = ROOT / "data" / f"{case}.json"
        graph = load(path)
        assert set(graph) == {"tensors", "ops", "edges"}
        ts, ops, es = graph["tensors"], graph["ops"], graph["edges"]
        assert all(isinstance(t["size"], int) and t["size"] >= 0 and "pos" in t for t in ts)
        pos_counts = Counter(t["pos"] for t in ts)
        raw_cases.append({"case": case, "split": phase_by_case[case], "ops": len(ops), "edges": len(es), "tensors": len(ts), "tensor_bytes_sum": sum(t["size"] for t in ts), "ddr_tensor_bytes_sum": sum(t["size"] for t in ts if t["pos"] == "DDR"), "ddr_tensor_count": pos_counts["DDR"], "l1_tensor_count": pos_counts["L1"], "ub_tensor_count": pos_counts["UB"], "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        raw_tensors.extend({"case": case, "tensor_id": t["id"], "pos": t["pos"], "size_bytes": t["size"]} for t in ts)
    write_csv("q3_raw_case_graph.csv", raw_cases)
    write_csv("q3_raw_tensors.csv", raw_tensors)

    process = []
    for branch, folder in (("ordering", "problem3_g2_ordering_v2"), ("mapping", "problem3_final_mapping_v2")):
        for path in sorted((ROOT / "artifacts/artifacts" / folder / "invocations").glob("*.json")):
            d = load(path)
            rr = d["progress"]
            process.append({"branch": branch, "phase": d["phase"], "groups": len(rr), "cases": len({r["case"] for r in rr}), "strict_improved_groups": sum(bool(r["strict_improvement"]) for r in rr), "strict_improved_cases": len({r["case"] for r in rr if r["strict_improvement"]}), "official_calls_this_invocation": d["official_calls_this_invocation"], "failed_groups": d["failed_groups"], "invocation_file": str(path.relative_to(ROOT)), "overlaps_development": d["phase"] == "smoke"})
    write_csv("q3_process_invocations.csv", process)

    effect = []
    for r in pairs:
        effect.append({"case": r["case"], "split": phase_by_case[r["case"]], "cores": r["cores"], "baseline_no_l2_makespan_cycles": r["baseline_no_l2_makespan"], "baseline_l2_makespan_cycles": r["baseline_l2_makespan"], "final_no_l2_makespan_cycles": r["final_no_l2_makespan"], "final_l2_makespan_cycles": r["final_l2_makespan"], "hardware_baseline_ratio": r["hardware_speedup_baseline_mean_of_ratios_member"], "hardware_final_ratio": r["hardware_speedup_final_mean_of_ratios_member"], "schedule_no_l2_ratio": r["no_l2_plan_tuning_speedup_mean_of_ratios_member"], "schedule_l2_ratio": r["l2_plan_tuning_speedup_mean_of_ratios_member"], "l2_strict_improvement": int(r["final_l2_makespan"]) < int(r["baseline_l2_makespan"])})
    write_csv("q3_effects_case_core.csv", effect)
    write_csv("q3_scaling_curve_source.csv", curves)

    summary = load(REPORT / "summary.json")
    ablation = []
    for c in summary["curves"]:
        ablation.append({"cores": c["cores"], "n_cases": c["cases"], "mean_baseline_no_l2_cycles": c["mean_baseline_no_l2_makespan"], "mean_baseline_l2_cycles": c["mean_baseline_l2_makespan"], "mean_final_no_l2_cycles": c["mean_final_no_l2_makespan"], "mean_final_l2_cycles": c["mean_final_l2_makespan"], "mean_case_hardware_baseline_ratio": c["mean_of_case_ratios_hardware_speedup_baseline"], "mean_case_hardware_final_ratio": c["mean_of_case_ratios_hardware_speedup_final"], "mean_case_schedule_no_l2_ratio": c["mean_of_case_ratios_no_l2_plan_tuning_speedup"], "mean_case_schedule_l2_ratio": c["mean_of_case_ratios_l2_plan_tuning_speedup"], "schedule_l2_strict_wins": c["wins_l2_plan_tuning_speedup"], "schedule_l2_mean_ratio_ci95_low": c["bootstrap_95ci_mean_l2_plan_tuning_speedup"][0], "schedule_l2_mean_ratio_ci95_high": c["bootstrap_95ci_mean_l2_plan_tuning_speedup"][1]})
    write_csv("q3_table_2x2_ablation.csv", ablation)

    typical = []
    for case, k, role in (("case_050", 4, "G1 hardware positive"), ("case_001", 2, "G1 hardware no gain"), ("case_067", 2, "G1 hardware negative")):
        evidence = load(TIMELINE / f"{case}_k{k}.json")
        r = next(x for x in pairs if x["case"] == case and int(x["cores"]) == k)
        ev = evidence["l2"]["cache_event_evidence"]
        cs = evidence["l2"]["cache_stats"]
        assert evidence["no_l2"]["makespan"] == int(r["baseline_no_l2_makespan"])
        assert evidence["l2"]["makespan"] == int(r["baseline_l2_makespan"])
        typical.append({"case": case, "cores": k, "role": role, "baseline_no_l2_cycles": evidence["no_l2"]["makespan"], "baseline_l2_cycles": evidence["l2"]["makespan"], "final_l2_cycles": int(r["final_l2_makespan"]), "copy_in_hits": cs["copy_in_hits"], "copy_in_misses": cs["copy_in_misses"], "copy_in_hit_bytes": cs["hit_bytes"], "copy_in_miss_bytes": cs["miss_bytes"], "copy_in_byte_hit_rate": cs["hit_bytes"]/(cs["hit_bytes"]+cs["miss_bytes"]), "fifo_evictions": ev["eviction_count"], "first_hit_cycle": ev["first_hit"]["time"] if ev["first_hit"] else "", "first_eviction_cycle": ev["first_eviction"]["time"] if ev["first_eviction"] else "", "event_resolution": "summary and selected first events only", "timeline_source": str((TIMELINE / f"{case}_k{k}.json").relative_to(ROOT))})
    write_csv("q3_table_typical_cases.csv", typical)
    print(f"PREPARED raw cases={len(raw_cases)} raw tensors={len(raw_tensors)} process={len(process)} paired={len(effect)} ablation={len(ablation)} typical={len(typical)}")


if __name__ == "__main__":
    main()
