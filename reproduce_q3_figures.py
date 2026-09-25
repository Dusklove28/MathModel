"""One-command Problem 3 figures, source tables and mechanical audits."""
from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def locate_skill_root() -> Path:
    """Find the optional math-modeling figure tools on the current machine."""
    configured = os.environ.get("MATH_MODELING_SKILL_ROOT")
    candidate = Path(configured).expanduser() if configured else Path.home() / ".codex/skills/math-modeling"
    if not (candidate / "tools/figure/scripts/setup_style.py").is_file():
        raise FileNotFoundError(
            f"math-modeling figure tools not found at {candidate}; "
            "set MATH_MODELING_SKILL_ROOT to the installed skill directory"
        )
    return candidate


def run(*args):
    completed = subprocess.run([sys.executable, *map(str, args)], cwd=ROOT, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def main():
    skill = locate_skill_root()
    run(ROOT / "prepare_q3_figures.py")
    profile = skill / "tools/figure/scripts/profile_data.py"
    for source, groups, output in (
        ("q3_raw_case_graph.csv", ["split"], "q3_profile_raw_cases.json"),
        ("q3_raw_tensors.csv", ["pos"], "q3_profile_raw_tensors.json"),
        ("q3_effects_case_core.csv", ["cores"], "q3_profile_effects.json"),
        ("q3_process_invocations.csv", ["branch", "phase"], "q3_profile_process.json"),
    ):
        command = [sys.executable, str(profile), str(ROOT / "results" / source), "--json"]
        for group in groups:
            command.extend(["--group", group])
        with (ROOT / "results" / output).open("w", encoding="utf-8") as handle:
            completed = subprocess.run(command, cwd=ROOT, stdout=handle, check=False)
        if completed.returncode:
            raise SystemExit(completed.returncode)
    run(ROOT / "plot_q3_figures.py")
    formal = sorted(path for path in (ROOT / "figures").iterdir() if path.is_file() and path.suffix.lower() in {".png", ".svg"})
    assert len(formal) == 20
    check = skill / "tools/figure/scripts/check_figure.py"
    audit = skill / "references/roles/编程手/scripts/figure_audit.py"
    with (ROOT / "results/q3_check_figure.txt").open("w", encoding="utf-8") as handle:
        result = subprocess.run([sys.executable, str(check), *map(str, formal), "--strict"], cwd=ROOT, stdout=handle, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    with (ROOT / "results/q3_figure_audit.json").open("w", encoding="utf-8") as handle:
        result = subprocess.run([sys.executable, str(audit), str(ROOT / "figures"), "--questions", "q3", "--strict"], cwd=ROOT, stdout=handle, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    print("Q3 FIGURES: PASS; 10 SVG/PNG pairs; both audits exit 0")


if __name__ == "__main__":
    main()
