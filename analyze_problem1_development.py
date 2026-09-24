"""Audit the locked Problem-1 development run without invoking the evaluator.

The development summary is reconciled with its CSV export.  Candidate-level
ablation is deliberately computed from the saved per-combination manifests,
because winner counts do not measure unique contribution.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


METHODS = ("single", "b0", "b1", "b2a_w4", "b2a_w8", "b2a_w16")
CSV_FIELDS = (
    "case", "cores", "winner", "unique_candidates", "evaluations",
    "cache_hits", "winner_makespan", "winner_added_bytes",
    "single_makespan", "single_added_bytes", "b0_makespan",
    "b0_added_bytes", "improvement_vs_single", "improvement_vs_b0",
    "total_wall_time", "evaluation_time", "max_memory_bytes",
    "failed_candidates",
)


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    temporary.replace(path)


def _parse_csv_value(text: str, expected: Any) -> Any:
    if isinstance(expected, bool):
        return text.lower() == "true"
    if isinstance(expected, int):
        return int(text)
    if isinstance(expected, float):
        return float(text)
    return text


def reconcile_summary_csv(
    summary: Mapping[str, Any], csv_path: Path,
) -> Dict[str, Any]:
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    json_rows = summary.get("results", [])
    mismatches: List[Dict[str, Any]] = []
    if len(csv_rows) == len(json_rows):
        for index, (csv_row, json_row) in enumerate(zip(csv_rows, json_rows)):
            for field in CSV_FIELDS:
                expected = json_row.get(field, "")
                try:
                    actual = _parse_csv_value(csv_row.get(field, ""), expected)
                except (TypeError, ValueError):
                    actual = csv_row.get(field, "")
                equal = (
                    math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12)
                    if isinstance(actual, (int, float))
                    and not isinstance(actual, bool)
                    and isinstance(expected, (int, float))
                    and not isinstance(expected, bool)
                    else actual == expected
                )
                if not equal:
                    mismatches.append({
                        "row": index + 2,
                        "field": field,
                        "csv": actual,
                        "json": expected,
                    })
    else:
        mismatches.append({
            "field": "row_count",
            "csv": len(csv_rows),
            "json": len(json_rows),
        })
    combinations = [(row.get("case"), row.get("cores")) for row in json_rows]
    return {
        "json_result_count": len(json_rows),
        "csv_row_count": len(csv_rows),
        "unique_combination_count": len(set(combinations)),
        "case_count": len({case for case, _ in combinations}),
        "cores": sorted({cores for _, cores in combinations}),
        "failed_combination_count": sum(
            bool(row.get("failed_candidates")) for row in json_rows),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "matched": not mismatches,
    }


def _valid_candidates(manifest: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    return [
        record for record in manifest.get("candidates", [])
        if record.get("status") == "evaluated"
        and isinstance(record.get("makespan"), int)
        and isinstance(record.get("added_copy_bytes"), int)
    ]


def _score(record: Mapping[str, Any]) -> Tuple[int, int]:
    return int(record["makespan"]), int(record["added_copy_bytes"])


def _best(
    records: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    priority = {name: index for index, name in enumerate(METHODS)}
    values = list(records)
    if not values:
        raise ValueError("no evaluated candidate")
    return min(values, key=lambda row: (*_score(row), priority[row["name"]]))


def load_development_manifests(
    summary: Mapping[str, Any], manifests_root: Path,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    loaded: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for row in summary["results"]:
        case = row["case"]
        cores = row["cores"]
        path = Path(manifests_root) / case / ("k{}".format(cores)) / "manifest.json"
        if not path.is_file():
            missing.append({"case": case, "cores": cores, "path": str(path.resolve())})
            continue
        manifest = _read_json(path)
        problems: List[str] = []
        if manifest.get("case") != case or manifest.get("cores") != cores:
            problems.append("case_or_core_mismatch")
        names = [record.get("name") for record in manifest.get("candidates", [])]
        if tuple(names) != METHODS:
            problems.append("candidate_definition_mismatch")
        winner = manifest.get("winner") or {}
        if (
            winner.get("name") != row["winner"]
            or winner.get("makespan") != row["winner_makespan"]
            or winner.get("added_copy_bytes") != row["winner_added_bytes"]
        ):
            problems.append("summary_winner_mismatch")
        if problems:
            missing.append({
                "case": case,
                "cores": cores,
                "path": str(path.resolve()),
                "problems": problems,
            })
            continue
        loaded.append({"case": case, "cores": cores, "path": path, "manifest": manifest})
    return loaded, missing


def build_ablation(
    manifests: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    candidate_rows: List[Dict[str, Any]] = []
    detail_rows: List[Dict[str, Any]] = []
    for item in manifests:
        case = item["case"]
        cores = item["cores"]
        manifest = item["manifest"]
        valid = _valid_candidates(manifest)
        best = _best(valid)
        best_score = _score(best)
        by_name = {record["name"]: record for record in manifest["candidates"]}
        for name in METHODS:
            record = by_name[name]
            candidate_rows.append({
                "case": case,
                "cores": cores,
                "method": name,
                "status": record.get("status"),
                "makespan": record.get("makespan"),
                "added_copy_bytes": record.get("added_copy_bytes"),
                "winner_by_priority": name == manifest["winner"]["name"],
                "matches_best_score": (
                    record.get("status") == "evaluated" and _score(record) == best_score),
                "deduplicated": bool(record.get("deduplicated")),
                "canonical_candidate": record.get("canonical_candidate"),
                "cache_hit": bool(record.get("cache_hit")),
                "evaluation_time": record.get("evaluation_time"),
            })
            alternatives = [candidate for candidate in valid if candidate["name"] != name]
            if record.get("status") != "evaluated" or not alternatives:
                classification = "candidate_failed"
                alternative: Mapping[str, Any] = {}
                loss_makespan = None
                loss_added = None
            else:
                alternative = _best(alternatives)
                alternative_score = _score(alternative)
                record_score = _score(record)
                loss_makespan = alternative_score[0] - best_score[0]
                loss_added = alternative_score[1] - best_score[1]
                if record_score != best_score:
                    classification = "not_best"
                elif alternative_score == best_score:
                    classification = "exact_tie"
                elif alternative_score[0] > best_score[0]:
                    classification = "strict_makespan_improvement"
                elif (
                    alternative_score[0] == best_score[0]
                    and alternative_score[1] > best_score[1]
                ):
                    classification = "movement_only_improvement"
                else:
                    raise AssertionError("invalid lexicographic ablation state")
            detail_rows.append({
                "case": case,
                "cores": cores,
                "omitted_method": name,
                "classification": classification,
                "candidate_makespan": record.get("makespan"),
                "candidate_added_copy_bytes": record.get("added_copy_bytes"),
                "original_best_method": best["name"],
                "original_best_makespan": best_score[0],
                "original_best_added_copy_bytes": best_score[1],
                "alternative_method": alternative.get("name"),
                "alternative_makespan": alternative.get("makespan"),
                "alternative_added_copy_bytes": alternative.get("added_copy_bytes"),
                "makespan_loss": loss_makespan,
                "added_copy_bytes_change": loss_added,
            })

    method_rows: List[Dict[str, Any]] = []
    for method in METHODS:
        rows = [row for row in detail_rows if row["omitted_method"] == method]
        counts = Counter(row["classification"] for row in rows)
        losses = [row["makespan_loss"] for row in rows if row["makespan_loss"] is not None]
        added = [
            row["added_copy_bytes_change"]
            for row in rows if row["added_copy_bytes_change"] is not None
        ]
        winner_count = sum(
            row["method"] == method and row["winner_by_priority"]
            for row in candidate_rows
        )
        method_rows.append({
            "method": method,
            "combinations": len(rows),
            "winner_count_by_priority": winner_count,
            "strict_makespan_improvement": counts["strict_makespan_improvement"],
            "movement_only_improvement": counts["movement_only_improvement"],
            "exact_tie": counts["exact_tie"],
            "not_best": counts["not_best"],
            "candidate_failed": counts["candidate_failed"],
            "score_loss_when_removed": (
                counts["strict_makespan_improvement"]
                + counts["movement_only_improvement"]),
            "total_makespan_loss": sum(losses),
            "mean_makespan_loss": (sum(losses) / len(losses) if losses else None),
            "max_makespan_loss": (max(losses) if losses else None),
            "total_added_copy_bytes_change": sum(added),
        })
    return candidate_rows, detail_rows, method_rows


def aggregate_by_case(detail_rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for row in detail_rows:
        grouped[(row["case"], row["omitted_method"])].append(row)
    output: List[Dict[str, Any]] = []
    for (case, method), rows in sorted(grouped.items()):
        counts = Counter(row["classification"] for row in rows)
        output.append({
            "case": case,
            "method": method,
            "cores_count": len(rows),
            "strict_makespan_improvement": counts["strict_makespan_improvement"],
            "movement_only_improvement": counts["movement_only_improvement"],
            "exact_tie": counts["exact_tie"],
            "not_best": counts["not_best"],
            "candidate_failed": counts["candidate_failed"],
            "total_makespan_loss": sum(
                row["makespan_loss"] or 0 for row in rows),
            "max_makespan_loss": max(
                (row["makespan_loss"] or 0 for row in rows), default=0),
            "total_added_copy_bytes_change": sum(
                row["added_copy_bytes_change"] or 0 for row in rows),
        })
    return output


def make_scatter(candidate_rows: Sequence[Mapping[str, Any]], output_path: Path) -> None:
    import matplotlib.pyplot as plt

    colors = {
        "single": "#4c78a8",
        "b0": "#f58518",
        "b1": "#54a24b",
        "b2a_w4": "#e45756",
        "b2a_w8": "#72b7b2",
        "b2a_w16": "#b279a2",
    }
    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    for cores, axis in zip((2, 3, 4, 5), axes.flat):
        for method in METHODS:
            rows = [
                row for row in candidate_rows
                if row["cores"] == cores and row["method"] == method
                and row["status"] == "evaluated"
            ]
            axis.scatter(
                [row["added_copy_bytes"] for row in rows],
                [row["makespan"] for row in rows],
                label=method,
                color=colors[method],
                alpha=0.72,
                s=34,
            )
        winners = [
            row for row in candidate_rows
            if row["cores"] == cores and row["winner_by_priority"]
        ]
        axis.scatter(
            [row["added_copy_bytes"] for row in winners],
            [row["makespan"] for row in winners],
            facecolors="none", edgecolors="black", linewidths=1.0, s=80,
            label="selected",
        )
        axis.set_xscale("symlog", linthresh=1.0)
        axis.set_yscale("log")
        axis.set_title("{} cores".format(cores))
        axis.set_xlabel("Added copy bytes (symlog)")
        axis.set_ylabel("Makespan cycles (log)")
        axis.grid(True, which="major", linewidth=0.5, alpha=0.25)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.subplots_adjust(top=0.86, bottom=0.08, hspace=0.34, wspace=0.24)
    fig.legend(
        handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.925),
        ncol=7, frameon=False)
    fig.suptitle(
        "Problem 1 development candidates: makespan and data movement",
        y=0.975)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def analyze(
    summary_path: Path,
    csv_path: Path,
    manifests_root: Path,
    output_dir: Path,
    *,
    require_complete: bool = True,
) -> Dict[str, Any]:
    summary = _read_json(summary_path)
    reconciliation = reconcile_summary_csv(summary, csv_path)
    loaded, missing = load_development_manifests(summary, manifests_root)
    audit = {
        "schema_version": 1,
        "summary_path": str(summary_path.resolve()),
        "summary_sha256": _sha256(summary_path),
        "csv_path": str(csv_path.resolve()),
        "csv_sha256": _sha256(csv_path),
        "manifests_root": str(manifests_root.resolve()),
        "reconciliation": reconciliation,
        "expected_manifest_count": len(summary.get("results", [])),
        "loaded_manifest_count": len(loaded),
        "missing_or_invalid_manifest_count": len(missing),
        "missing_or_invalid_manifests": missing,
        "complete": reconciliation["matched"] and not missing,
    }
    _write_json(output_dir / "development_input_audit.json", audit)
    if not reconciliation["matched"]:
        raise ValueError("development JSON and CSV do not match")
    if missing and require_complete:
        raise FileNotFoundError(
            "{} of {} development manifests are missing or invalid; see {}".format(
                len(missing), len(summary["results"]),
                output_dir / "development_input_audit.json"))

    candidate_rows, detail_rows, method_rows = build_ablation(loaded)
    case_rows = aggregate_by_case(detail_rows)
    _write_csv(output_dir / "candidate_records.csv", candidate_rows, (
        "case", "cores", "method", "status", "makespan",
        "added_copy_bytes", "winner_by_priority", "matches_best_score",
        "deduplicated", "canonical_candidate", "cache_hit", "evaluation_time",
    ))
    _write_csv(output_dir / "ablation_details.csv", detail_rows, (
        "case", "cores", "omitted_method", "classification",
        "candidate_makespan", "candidate_added_copy_bytes",
        "original_best_method", "original_best_makespan",
        "original_best_added_copy_bytes", "alternative_method",
        "alternative_makespan", "alternative_added_copy_bytes",
        "makespan_loss", "added_copy_bytes_change",
    ))
    _write_csv(output_dir / "ablation_by_method.csv", method_rows, tuple(method_rows[0]))
    _write_csv(output_dir / "ablation_by_case.csv", case_rows, tuple(case_rows[0]))
    scatter_path = output_dir / "candidate_makespan_vs_added_bytes.png"
    make_scatter(candidate_rows, scatter_path)
    result = {
        **audit,
        "candidate_record_count": len(candidate_rows),
        "ablation_detail_count": len(detail_rows),
        "method_ablation": method_rows,
        "outputs": {
            "candidate_records": str((output_dir / "candidate_records.csv").resolve()),
            "ablation_details": str((output_dir / "ablation_details.csv").resolve()),
            "ablation_by_method": str((output_dir / "ablation_by_method.csv").resolve()),
            "ablation_by_case": str((output_dir / "ablation_by_case.csv").resolve()),
            "scatter": str(scatter_path.resolve()),
        },
    }
    _write_json(output_dir / "development_analysis.json", result)
    return result


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze saved Problem-1 development candidates without evaluation")
    parser.add_argument(
        "--summary", default="problem1_development_summary.json")
    parser.add_argument(
        "--csv", default="problem1_development_summary.csv")
    parser.add_argument(
        "--manifests-root", default="artifacts/problem1_candidates")
    parser.add_argument(
        "--output-dir", default="artifacts/problem1_development_analysis")
    parser.add_argument(
        "--allow-partial", action="store_true",
        help="write partial diagnostics when manifests are missing")
    args = parser.parse_args(argv)
    try:
        result = analyze(
            Path(args.summary), Path(args.csv), Path(args.manifests_root),
            Path(args.output_dir), require_complete=not args.allow_partial)
    except (FileNotFoundError, ValueError) as error:
        print("[ANALYSIS INPUT ERROR] {}".format(error))
        return 2
    print("development combinations: {}".format(
        result["reconciliation"]["json_result_count"]))
    print("candidate records: {}".format(result["candidate_record_count"]))
    print("complete: {}".format(result["complete"]))
    print("output: {}".format(Path(args.output_dir).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
