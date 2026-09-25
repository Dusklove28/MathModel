"""Read-only G0 audit of the frozen Problem-2 Stage-1 winner plans."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


DEPENDENCIES = (
    "contest_io.py", "evaluation_validation.py",
    "multicore_cut_evaluate_problem_1.py",
    "multicore_cut_evaluate_problem_2.py",
    "multicore_cut_evaluate_problem_3.py",
    "schedule_step1.py", "schedule_step2.py", "schedule_step3.py",
    "stub_multicore_cut_and_schedule.py",
)


def sha256_file(path: Path, *, normalize_newlines: bool = False) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if normalize_newlines:
            digest.update(stream.read().replace(b"\r\n", b"\n"))
        else:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def git_head(repo: Path) -> str | None:
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
            text=True, check=False,
        )
    except OSError:
        # Offline ZIP deployments may have neither Git nor a .git directory.
        return None
    return probe.stdout.strip() if probe.returncode == 0 else None


def internal_plan_sha256(plan: dict) -> str:
    """Stage-1 hashed Python integer mapping keys before JSON serialization."""
    converted = dict(plan)
    converted["node_to_subgraph"] = {
        int(key): value for key, value in plan["node_to_subgraph"].items()
    }
    encoded = json.dumps(converted, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def serialized_plan_sha256(plan: dict) -> str:
    """Inherited Stage-1 candidates retained JSON string mapping keys."""
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def audit(repo: Path, baseline: Path) -> dict:
    repo, baseline = repo.resolve(), baseline.resolve()
    code = repo / "code"
    config = repo / "data" / "config.txt"
    official_hashes = {
        name: {
            "raw_sha256": sha256_file(code / name),
            "lf_sha256": sha256_file(code / name, normalize_newlines=True),
        }
        for name in DEPENDENCIES
    }
    missing, mismatches = [], []
    group_count = plan_count = result_count = 0
    graph_hash_matches = plan_hash_matches = result_hash_matches = 0
    baseline_identity = json.loads((baseline / "run_identity.json").read_text(encoding="utf-8"))
    local_config_lf = sha256_file(config, normalize_newlines=True)
    if local_config_lf != baseline_identity.get("config_sha256"):
        mismatches.append("config.txt LF SHA256 differs from Stage-1 baseline")
    for name, expected in baseline_identity.get("evaluator_file_sha256", {}).items():
        if name not in official_hashes or official_hashes[name]["lf_sha256"] != expected:
            mismatches.append(f"official dependency LF SHA256: {name}")
    for case_number in range(1, 101):
        case = f"case_{case_number:03d}"
        graph_path = repo / "data" / f"{case}.json"
        if not graph_path.is_file():
            missing.append(str(graph_path.relative_to(repo)))
            continue
        graph_hash = sha256_file(graph_path)
        for cores in range(1, 6):
            relative = Path("groups") / case / f"k{cores}"
            manifest_path = baseline / relative.with_suffix(".json")
            plan_path = baseline / relative / "final_multicore_res.json"
            result_path = baseline / relative / "final_official_evaluation.json"
            if not manifest_path.is_file():
                missing.append(str(manifest_path.relative_to(baseline)))
                continue
            group_count += 1
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") != "success":
                mismatches.append(f"{case}/k{cores}: status={manifest.get('status')}")
            if manifest.get("case") != case or manifest.get("cores") != cores:
                mismatches.append(f"{case}/k{cores}: case/core identity")
            for key in ("config_sha256", "evaluator_sha256",
                        "problem2_implementation_sha256"):
                if manifest.get(key) != baseline_identity.get(key):
                    mismatches.append(f"{case}/k{cores}: {key} identity")
            if manifest.get("evaluator_file_sha256") != baseline_identity.get(
                    "evaluator_file_sha256"):
                mismatches.append(f"{case}/k{cores}: evaluator file identity")
            if manifest.get("graph_sha256") == graph_hash:
                graph_hash_matches += 1
            else:
                mismatches.append(f"{case}/k{cores}: graph SHA256")
            for path, hash_key, label in (
                (plan_path, "final_plan_file_sha256", "plan"),
                (result_path, "final_official_result_file_sha256", "result"),
            ):
                if not path.is_file():
                    missing.append(str(path.relative_to(baseline)))
                    continue
                actual = sha256_file(path)
                if label == "plan":
                    plan_count += 1
                    plan_hash_matches += actual == manifest.get(hash_key)
                else:
                    result_count += 1
                    result_hash_matches += actual == manifest.get(hash_key)
                if actual != manifest.get(hash_key):
                    mismatches.append(f"{case}/k{cores}: {label} SHA256")
            if plan_path.is_file():
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                expected_plan_hash = manifest.get("winner", {}).get("plan_hash")
                if expected_plan_hash not in (
                        internal_plan_sha256(plan), serialized_plan_sha256(plan)):
                    mismatches.append(f"{case}/k{cores}: winner canonical plan hash")
    counts = {
        "expected_groups": 500,
        "manifest_files": group_count,
        "winner_plan_files": plan_count,
        "winner_official_result_files": result_count,
        "matching_graph_hashes": graph_hash_matches,
        "matching_plan_file_hashes": plan_hash_matches,
        "matching_result_file_hashes": result_hash_matches,
    }
    return {
        "schema_version": 1,
        "repo": str(repo), "git_head": git_head(repo),
        "baseline": str(baseline),
        "baseline_git_head": baseline_identity.get("git_head"),
        "baseline_runner_sha256": baseline_identity.get("runner_sha256"),
        "baseline_solver_sha256": baseline_identity.get("solver_sha256"),
        "local_runner_lf_sha256": sha256_file(repo / "run_problem2_baseline.py", normalize_newlines=True),
        "local_solver_lf_sha256": sha256_file(repo / "solver_problem2.py", normalize_newlines=True),
        "baseline_config_sha256": baseline_identity.get("config_sha256"),
        "local_config": {
            "raw_sha256": sha256_file(config),
            "lf_sha256": local_config_lf,
        },
        "baseline_official_file_sha256": baseline_identity.get("evaluator_file_sha256"),
        "local_official_dependencies": official_hashes,
        "counts": counts, "missing": missing, "mismatches": mismatches,
        "passed": not missing and not mismatches and all(
            value == 500 for value in list(counts.values())[1:]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    baseline = args.baseline or repo / "artifacts" / "problem2_stage1_baseline_full"
    report = audit(repo, baseline)
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
