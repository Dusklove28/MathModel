"""Freeze Problem-3 dev/validation/holdout cases without reading L2 results."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from audit_problem3_assets import sha256_file


DEV_CASES = (
    "case_001", "case_016", "case_024", "case_044", "case_047",
    "case_048", "case_050", "case_051", "case_058", "case_064",
    "case_067", "case_100",
)
SEED = 20260925


def make_split(repo: Path, baseline: Path) -> dict:
    remaining = [f"case_{i:03d}" for i in range(1, 101)
                 if f"case_{i:03d}" not in DEV_CASES]
    q2_makespan = {}
    result_hashes = {}
    for case in remaining:
        result_path = baseline / "groups" / case / "k1" / "final_official_evaluation.json"
        manifest_path = baseline / "groups" / case / "k1.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result_hash = sha256_file(result_path)
        if result_hash != manifest["final_official_result_file_sha256"]:
            raise ValueError(f"Stage-1 k1 official result SHA256 mismatch: {case}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        q2_makespan[case] = result["makespan"]
        result_hashes[case] = result_hash
    ordered = sorted(remaining, key=lambda case: (q2_makespan[case], case))
    strata = [ordered[:29], ordered[29:58], ordered[58:]]
    rng = random.Random(SEED)
    validation = sorted(case for stratum in strata
                        for case in rng.sample(stratum, 6))
    holdout = sorted(set(remaining) - set(validation))
    if len(validation) != 18 or len(holdout) != 70:
        raise AssertionError("split counts are not 12/18/70")
    return {
        "schema_version": 1,
        "seed": SEED,
        "rule": "Exclude 12 fixed dev cases; sort remaining 88 by frozen Problem-2 Stage-1 k1 makespan then case ID; split rank positions 0:29, 29:58, 58:88; sample 6 without replacement per stratum using Python random.Random(seed); remaining 70 are final holdout. No Problem-3 result is read.",
        "stratum_sizes": [len(x) for x in strata],
        "development_cases": list(DEV_CASES),
        "validation_cases": validation,
        "holdout_cases": holdout,
        "selection_source": "problem2_stage1_baseline_full/groups/<case>/k1/final_official_evaluation.json",
        "source_result_file_sha256": result_hashes,
        "stratum_by_case": {case: index for index, stratum in enumerate(strata)
                            for case in stratum},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    baseline = args.baseline or repo / "artifacts" / "problem2_stage1_baseline_full"
    output = args.output or repo / "problem3_preregistered_split.json"
    split = make_split(repo, baseline)
    serialized = json.dumps(split, ensure_ascii=False, indent=2) + "\n"
    if output.exists():
        if output.read_text(encoding="utf-8") != serialized:
            raise ValueError(f"frozen split exists with different content: {output}")
    else:
        output.write_text(serialized, encoding="utf-8")
    print(f"development={len(split['development_cases'])} "
          f"validation={len(split['validation_cases'])} "
          f"holdout={len(split['holdout_cases'])} seed={SEED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
