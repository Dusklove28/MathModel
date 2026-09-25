#!/usr/bin/env python3
"""Rebuild Stage-2 reports from frozen groups without official evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from candidate_manager_problem1 import sha256_file
from candidate_manager_problem2 import json_sha256
from candidate_manager_problem2_stage2 import (
    MAPPING_POLICIES,
    load_verified_stage1_selected_references,
    stage2_implementation_hash,
)
from contest_io import _read_json
from run_problem2_stage2_mapping import summarize


def _load_frozen_groups(stage2_root: Path) -> tuple[
        List[Dict[str, Any]], Dict[str, Any], Dict[str, str]]:
    identity_path = stage2_root / "run_identity.json"
    summary_path = stage2_root / "summary.json"
    if not identity_path.is_file() or not summary_path.is_file():
        raise ValueError("Stage-2 run identity or summary is missing")
    identity = _read_json(identity_path)
    cases = tuple(str(case) for case in identity["cases"])
    cores = tuple(int(core) for core in identity["cores"])
    families = tuple(str(family) for family in identity["families"])
    allowed_pairs = {
        (str(case), int(core)) for case, core in identity.get("pairs", [])
    }
    groups: List[Dict[str, Any]] = []
    group_hashes: Dict[str, str] = {}
    for case in cases:
        for cores_value in cores:
            if allowed_pairs and (case, cores_value) not in allowed_pairs:
                continue
            for family in families:
                relative = Path("groups") / case / "k{}".format(
                    cores_value) / (family + ".json")
                path = stage2_root / relative
                if not path.is_file():
                    raise ValueError("frozen group is missing: {}".format(path))
                group = _read_json(path)
                expected = (case, cores_value, family, "success")
                actual = (
                    str(group.get("case")), int(group.get("cores")),
                    str(group.get("partition_family")), str(group.get("status")),
                )
                if actual != expected:
                    raise ValueError(
                        "frozen group identity mismatch: {}".format(path))
                found = dict(group)
                found["_resume"] = True
                groups.append(found)
                group_hashes[str(relative).replace("\\", "/")] = sha256_file(path)
    return groups, identity, group_hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument(
        "--baseline", type=Path,
        default=Path("artifacts/problem2_stage1_baseline_cold_audit"))
    parser.add_argument("--stage2", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    repo = args.repo.resolve()
    baseline = (repo / args.baseline).resolve() if not args.baseline.is_absolute() else args.baseline.resolve()
    stage2 = (repo / args.stage2).resolve() if not args.stage2.is_absolute() else args.stage2.resolve()
    output = ((stage2 / "reporting_v2") if args.output is None else
              ((repo / args.output).resolve()
               if not args.output.is_absolute() else args.output.resolve()))
    groups, source_identity, group_hashes = _load_frozen_groups(stage2)
    pairs = sorted({(str(group["case"]), int(group["cores"])) for group in groups})
    selected = load_verified_stage1_selected_references(
        graph_root=repo / "data",
        config_path=repo / "data" / "config.txt",
        baseline_root=baseline,
        pairs=pairs,
    )
    present_policies = {
        str(candidate.get("policy"))
        for group in groups
        for candidate in group.get("candidates", [])[1:]
        if candidate.get("policy")
    }
    mapping_policies = tuple(
        policy for policy in MAPPING_POLICIES if policy in present_policies)
    if not mapping_policies:
        raise ValueError("frozen groups contain no mapping policies")
    implementation_hash, implementation_files = stage2_implementation_hash()
    report_identity = {
        "schema_version": 1,
        "kind": "problem2_stage2_offline_report_rebuild",
        "official_evaluator_invoked": False,
        "source_stage2_root": str(stage2),
        "source_run_identity_file_sha256": sha256_file(
            stage2 / "run_identity.json"),
        "source_summary_file_sha256": sha256_file(stage2 / "summary.json"),
        "source_group_file_sha256": group_hashes,
        "source_group_set_sha256": json_sha256(group_hashes),
        "source_run_identity": source_identity,
        "stage1_selected_results": str(selected.selected_results_path),
        "stage1_selected_results_file_sha256": (
            selected.selected_results_file_hash),
        "stage1_selected_reference_sha256": selected.selected_reference_hash,
        "reporter_sha256": sha256_file(Path(__file__).resolve()),
        "reporting_runner_sha256": sha256_file(
            Path(__file__).resolve().parent / "run_problem2_stage2_mapping.py"),
        "reporting_implementation_sha256": implementation_hash,
        "reporting_implementation_file_sha256": implementation_files,
        "mapping_policies": list(mapping_policies),
    }
    report_identity["report_identity_sha256"] = json_sha256(report_identity)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report_identity.json").write_text(
        json.dumps(report_identity, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    source_summary = _read_json(stage2 / "summary.json")
    summary = summarize(
        output=output,
        groups=groups,
        expected_groups=len(groups),
        preset=str(source_identity["preset"]),
        mapping_policies=mapping_policies,
        stage1_selected=selected.references,
        run_identity=report_identity,
        wall_seconds=float(source_summary.get("wall_seconds", 0.0)),
    )
    print(json.dumps({
        "output": str(output),
        "groups": len(groups),
        "case_core_cells": summary["case_core_cells"],
        "case_core_selection": summary["case_core_selection"],
        "policy_case_core_selection": summary["policy_case_core_selection"],
        "gate_passed": summary["continue_gate"]["passed"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
