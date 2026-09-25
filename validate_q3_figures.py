"""Fail-fast integrity gate for Problem 3 figure inputs."""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "artifacts/artifacts/problem3_report_final"


def read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def main() -> None:
    with (REPORT / "case_core_2x2.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with (REPORT / "curve_1_to_5.csv").open(encoding="utf-8-sig", newline="") as handle:
        curves = list(csv.DictReader(handle))
    summary = read_json(REPORT / "summary.json")
    split = read_json(ROOT / "problem3_preregistered_split.json")
    keys = {(r["case"], int(r["cores"])) for r in rows}
    cases = {f"case_{i:03d}" for i in range(1, 101)}
    assert len(rows) == len(keys) == 500
    assert keys == {(case, k) for case in cases for k in range(1, 6)}
    assert len(curves) == 5 and {int(r["cores"]) for r in curves} == set(range(1, 6))
    assert summary["result_rows"] == 500 and summary["case_count"] == 100
    groups = [set(split[f"{name}_cases"]) for name in ("development", "validation", "holdout")]
    assert list(map(len, groups)) == [12, 18, 70]
    assert groups[0].isdisjoint(groups[1]) and groups[0].isdisjoint(groups[2]) and groups[1].isdisjoint(groups[2])
    assert set.union(*groups) == cases and split["seed"] == 20260925
    assert all(r["baseline_legal"] == "True" and r["final_legal"] == "True" for r in rows)
    assert all(int(r[x]) > 0 for r in rows for x in ("baseline_no_l2_makespan", "baseline_l2_makespan", "final_no_l2_makespan", "final_l2_makespan"))
    changed = [r for r in rows if r["plan_changed"] == "True"]
    wins = [r for r in rows if int(r["final_l2_makespan"]) < int(r["baseline_l2_makespan"])]
    losses = [r for r in rows if int(r["final_l2_makespan"]) > int(r["baseline_l2_makespan"])]
    hold = [r for r in rows if r["case"] in groups[2] and int(r["cores"]) > 1]
    hold_wins = [r for r in hold if int(r["final_l2_makespan"]) < int(r["baseline_l2_makespan"])]
    assert len(changed) == 208 and len(wins) == 180 and not losses
    assert len(hold) == 280 and len(hold_wins) == 133
    assert len({r["case"] for r in wins}) == 80 and len({r["case"] for r in hold_wins}) == 58
    assert all(r["plan_changed"] == "False" for r in rows if int(r["cores"]) == 1)
    print("FINAL: 500 unique groups; 100 x 1-5; split 12/18/70; legal 500/500; changed 208; L2 wins 180; L2 losses 0; holdout wins 133/280")

    expected = {
        "ordering": [("smoke", 12, 4, 15), ("development", 48, 9, 40), ("validation", 72, 23, 84), ("holdout", 280, 85, 385)],
        "mapping": [("smoke", 12, 3, 11), ("development", 48, 3, 18), ("validation", 72, 24, 61), ("holdout", 280, 65, 242)],
    }
    for branch, folder in (("ordering", "problem3_g2_ordering_v2"), ("mapping", "problem3_final_mapping_v2")):
        files = sorted((ROOT / "artifacts/artifacts" / folder / "invocations").glob("*.json"))
        assert len(files) == 4
        got = []
        for path in files:
            d = read_json(path)
            records = d["progress"]
            assert len(records) == len({(r["case"], int(r["cores"])) for r in records})
            assert all(r["status"] == "success" for r in records)
            assert d["failed_groups"] == 0
            assert sum(int(r["official_calls"]) for r in records) == d["official_calls_this_invocation"]
            got.append((d["phase"], len(records), sum(bool(r["strict_improvement"]) for r in records), d["official_calls_this_invocation"]))
        assert got == expected[branch], (branch, got)
        assert sum(x[3] for x in got) == (524 if branch == "ordering" else 332)
        print(branch.upper(), got, "cumulative calls", sum(x[3] for x in got))
    print("PASS")


if __name__ == "__main__":
    main()
