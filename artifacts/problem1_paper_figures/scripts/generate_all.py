from __future__ import annotations

from plot_fig1_average_speedup import main as plot_fig1
from plot_fig2_speedup_distribution import main as plot_fig2
from plot_fig3_fallback_improvements import main as plot_fig3
from prepare_problem1_paper_data import main as prepare_data


def main() -> int:
    prepare_data()
    plot_fig1()
    plot_fig2()
    plot_fig3()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
