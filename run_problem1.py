"""Command-line runner for deterministic problem-1 B0/B1/B2A solvers."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from solver_problem1 import (
    B2A_WINDOW_CHOICES,
    OFFICIAL_CODE_DIR,
    solve_problem1_with_diagnostics,
)


PROJECT_DIR = Path(__file__).resolve().parent
if str(OFFICIAL_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(OFFICIAL_CODE_DIR))

from contest_io import _read_json, _write_json, run_problem_cli  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成确定性 B0/B1/B2A 多核方案并调用官方 problem 1 evaluator")
    parser.add_argument("graph", help="原始计算图 JSON")
    parser.add_argument("-n", "--num-cores", type=int, default=4)
    parser.add_argument(
        "--method", type=str.upper, choices=("B0", "B1", "B2A"), default="B0")
    parser.add_argument(
        "--windows", type=int, choices=B2A_WINDOW_CHOICES,
        help="B2A dependency-level window 数量")
    parser.add_argument(
        "-o", "--output", help="方案 JSON；默认 <graph>_multicore_res.json")
    parser.add_argument(
        "--diagnostics-output",
        help="诊断 JSON；默认在方案文件名后增加 _diagnostics")
    parser.add_argument(
        "--config", help="评估配置；默认使用计算图目录下的 config.txt")
    parser.add_argument("--evaluation-output", help="官方评估结果 JSON 路径")
    parser.add_argument("--trace-output", help="官方 Perfetto Trace JSON 路径")
    parser.add_argument("--log-output", help="官方简短日志路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.method == "B2A" and args.windows is None:
        parser.error("--method B2A requires --windows 4, 8, or 16")
    if args.method != "B2A" and args.windows is not None:
        parser.error("--windows is only valid with --method B2A")
    graph_path = Path(args.graph)
    plan_path = (
        Path(args.output)
        if args.output
        else Path(str(graph_path.with_suffix("")) + "_multicore_res.json")
    )
    config_path = Path(args.config) if args.config else graph_path.parent / "config.txt"
    diagnostics_path = (
        Path(args.diagnostics_output)
        if args.diagnostics_output
        else plan_path.with_name(plan_path.stem + "_diagnostics.json")
    )

    graph = _read_json(graph_path)
    started = time.perf_counter()
    plan, diagnostics = solve_problem1_with_diagnostics(
        graph, args.num_cores, method=args.method, windows=args.windows)
    solver_time = time.perf_counter() - started
    if set(plan) != {"node_to_subgraph", "core_schedules"}:
        raise RuntimeError("solver output must contain exactly the two official fields")
    _write_json(plan_path, plan)
    diagnostics.update({
        "case": graph_path.stem,
        "cores": args.num_cores,
        "solver_time_seconds": solver_time,
    })
    _write_json(diagnostics_path, diagnostics)

    subgraph_count = len(set(plan["node_to_subgraph"].values()))
    print(
        "{}: ops={} subgraphs={} cores={} solver_time={:.6f}s output={}".format(
            args.method, len(plan["node_to_subgraph"]), subgraph_count,
            args.num_cores, solver_time, plan_path))
    print(
        "quotient: edges={} longest_path_nodes={} longest_path_ratio={:.6f} "
        "ready_max={} ready_mean={:.6f}".format(
            diagnostics["quotient_dag_edge_count"],
            diagnostics["quotient_dag_longest_path_node_count"],
            diagnostics["longest_path_ratio"],
            diagnostics["max_ready_size"],
            diagnostics["mean_ready_size"]))
    print(
        "cores: distribution={} proxy_workload={} cross_subgraph_bytes={}".format(
            diagnostics["core_distribution"],
            diagnostics["core_proxy_workload"],
            diagnostics["total_cross_subgraph_bytes"]))
    if args.method == "B2A":
        print(
            "B2A partition: windows={}/{} levels={} components={}->{} "
            "components_per_window={} groups_per_window={}".format(
                diagnostics["actual_windows"], diagnostics["windows"],
                diagnostics["num_levels"],
                diagnostics["components_before_packing"],
                diagnostics["components_after_packing"],
                diagnostics["components_per_window"],
                diagnostics["groups_per_window"]))

    evaluator_args = [
        str(graph_path), str(plan_path), "--config", str(config_path),
    ]
    if args.evaluation_output:
        evaluator_args.extend(["--output", args.evaluation_output])
    if args.trace_output:
        evaluator_args.extend(["--trace-output", args.trace_output])
    if args.log_output:
        evaluator_args.extend(["--log-output", args.log_output])

    status = run_problem_cli(1, evaluator_args)
    if status != 0:
        return status

    result_path = (
        Path(args.evaluation_output)
        if args.evaluation_output
        else Path(str(graph_path.with_suffix("")) + "_problem_1_res.json")
    )
    result = _read_json(result_path)
    movement = result.get("data_movement_bytes", {})
    diagnostics.update({
        "makespan": result["makespan"],
        "added_copy_bytes": movement.get("added_copy_bytes", 0),
    })
    _write_json(diagnostics_path, diagnostics)
    print(
        "{} metrics: makespan={} added_copy_bytes={} solver_time={:.6f}s "
        "diagnostics={}".format(
            args.method, result["makespan"],
            movement.get("added_copy_bytes", 0), solver_time,
            diagnostics_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
