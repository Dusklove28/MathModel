from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = OUTPUT_ROOT.parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.plot_style import PALETTE  # noqa: E402


def _skill_root() -> Path:
    root = Path(
        os.environ.get(
            "MATH_MODELING_SKILL_ROOT",
            Path.home() / ".codex" / "skills" / "math-modeling",
        )
    ).resolve()
    required = root / "tools" / "figure" / "scripts"
    if not required.is_dir():
        raise FileNotFoundError(
            "未找到 math-modeling Skill。请设置环境变量 "
            f"MATH_MODELING_SKILL_ROOT；当前尝试路径：{root}"
        )
    return root


SKILL_ROOT = _skill_root()
FIGURE_TOOL_DIR = SKILL_ROOT / "tools" / "figure" / "scripts"
if str(FIGURE_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(FIGURE_TOOL_DIR))

from export_figure import export_figure  # noqa: E402
from layout_tools import add_panel_labels, finalize_figure  # noqa: E402
from setup_style import setup_style  # noqa: E402
from visual_qa import audit_layout, print_report, render_preview  # noqa: E402


def configure_style() -> dict:
    """Configure a compact Chinese-paper style at the final physical size."""
    info = setup_style(
        journal="general",
        lang="zh",
        use_sciplots=False,
        serif_for_zh=False,
        constrained_layout=True,
    )
    plt.rcParams.update(
        {
            "font.size": 8.0,
            "axes.labelsize": 8.5,
            "axes.titlesize": 9.5,
            "xtick.labelsize": 7.5,
            "ytick.labelsize": 7.5,
            "legend.fontsize": 7.5,
            "lines.linewidth": 1.35,
            "lines.markersize": 5.0,
            "axes.linewidth": 0.7,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "xtick.major.size": 3.0,
            "ytick.major.size": 3.0,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": False,
            "axes.unicode_minus": False,
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "savefig.facecolor": "white",
            "figure.facecolor": "white",
            "svg.hashsalt": "problem1-paper-figures-2026",
        }
    )
    return info


def add_light_y_grid(ax) -> None:
    ax.grid(axis="y", color="#D9D9D9", linewidth=0.55, linestyle=(0, (2, 2)))
    ax.set_axisbelow(True)


def add_light_x_grid(ax) -> None:
    ax.grid(axis="x", color="#D9D9D9", linewidth=0.55, linestyle=(0, (2, 2)))
    ax.set_axisbelow(True)


def audit_and_export(
    fig,
    *,
    stem: str,
    size_inches: tuple[float, float],
    dpi: int = 600,
) -> dict:
    """Run the Skill's layout QA, render a preview, and export SVG/PNG."""
    figure_dir = OUTPUT_ROOT / "figures"
    preview_dir = OUTPUT_ROOT / "qa" / "previews"
    report_dir = OUTPUT_ROOT / "qa" / "programmatic"
    figure_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    layout_strategy = finalize_figure(fig, prefer="constrained")
    preview_path = preview_dir / f"{stem}_preview.png"
    render_preview(fig, str(preview_path), dpi=180)
    issues = audit_layout(fig)
    verdict = print_report(issues)
    qa_record = {
        "stem": stem,
        "layout_strategy": layout_strategy,
        "programmatic_verdict": verdict,
        "issues": [{"severity": severity, "message": message} for severity, message in issues],
        "preview": str(preview_path),
        "size_inches": list(size_inches),
        "dpi": dpi,
    }
    qa_path = report_dir / f"{stem}_qa.json"
    qa_path.write_text(json.dumps(qa_record, ensure_ascii=False, indent=2), encoding="utf-8")
    if verdict != "PASS":
        raise RuntimeError(f"{stem} 程序视觉检查未通过：{verdict}，详见 {qa_path}")

    basename = figure_dir / stem
    outputs = export_figure(
        fig,
        basename=str(basename),
        formats=["pdf", "svg", "png", "tiff"],
        size_inches=size_inches,
        dpi=dpi,
        # The Skill helper's grayscale branch re-saves the colour PNG with a
        # tight bounding box.  Export colour formats once at the contract size,
        # then derive the grayscale audit image from that exact raster.
        grayscale_preview=False,
        # Preserve the physical size declared by each figure contract.  A tight
        # bounding box changes the exported canvas dimensions and makes layout
        # checks dependent on the current text extents.
        tight=False,
    )
    from PIL import Image

    color_png = Path(f"{basename}.png")
    grayscale_png = Path(f"{basename}_grayscale.png")
    with Image.open(color_png) as image:
        image.convert("L").save(grayscale_png, dpi=(dpi, dpi))
    outputs.append(str(grayscale_png))
    plt.close(fig)
    qa_record["outputs"] = outputs
    qa_path.write_text(json.dumps(qa_record, ensure_ascii=False, indent=2), encoding="utf-8")
    return qa_record


__all__ = [
    "OUTPUT_ROOT",
    "PALETTE",
    "PROJECT_ROOT",
    "SKILL_ROOT",
    "add_light_x_grid",
    "add_light_y_grid",
    "add_panel_labels",
    "audit_and_export",
    "configure_style",
]
