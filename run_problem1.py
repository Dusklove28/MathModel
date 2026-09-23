"""Command-line runner for the deterministic problem-1 B0 solver."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from solver_problem1 import solve_problem1


PROJECT_DIR = Path(__file__).resolve().parent
OFFICIAL_CODE_DIR = PROJECT_DIR / "code"
if str(OFFICIAL_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(OFFICIAL_CODE_DIR))

from contest_io import _read_json, _write_json, run_problem_cli  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成确定性 B0 多核方案并调用官方 problem 1 evaluator")
    parser.add_argument("graph", help="原始计算图 JSON")
    parser.add_argument("-n", "--num-cores", type=int, default=4)
    parser.add_argument(
        "-o", "--output", help="方案 JSON；默认 <graph>_multicore_res.json")
    parser.add_argument(
        "--config", help="评估配置；默认使用计算图目录下的 config.txt")
    parser.add_argument("--evaluation-output", help="官方评估结果 JSON 路径")
    parser.add_argument("--trace-output", help="官方 Perfetto Trace JSON 路径")
    parser.add_argument("--log-output", help="官方简短日志路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    graph_path = Path(args.graph)
    plan_path = (
        Path(args.output)
        if args.output
        else Path(str(graph_path.with_suffix("")) + "_multicore_res.json")
    )
    config_path = Path(args.config) if args.config else graph_path.parent / "config.txt"

    graph = _read_json(graph_path)
    started = time.perf_counter()
    plan = solve_problem1(graph, args.num_cores)
    solver_time = time.perf_counter() - started
    if set(plan) != {"node_to_subgraph", "core_schedules"}:
        raise RuntimeError("solver output must contain exactly the two official fields")
    _write_json(plan_path, plan)

    subgraph_count = len(set(plan["node_to_subgraph"].values()))
    print(
        "B0: ops={} subgraphs={} cores={} solver_time={:.6f}s output={}".format(
            len(plan["node_to_subgraph"]), subgraph_count,
            args.num_cores, solver_time, plan_path))

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
    print(
        "B0 metrics: makespan={} added_copy_bytes={} solver_time={:.6f}s".format(
            result["makespan"], movement.get("added_copy_bytes", 0), solver_time))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
