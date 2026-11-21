"""Shared plot styling.

Colours are the dataviz reference palette, validated with its colour-vision-deficiency checker
(all hard checks pass). Three of the lighter hues have < 3:1 contrast on the light surface,
so every figure carries visible text labels and the underlying numbers are saved as CSV.
"""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
               "#e87ba4", "#008300", "#6250d6", "#e34948"]

#: Fixed colour, marker and label per scheduler (colour follows the entity, never its rank).
SCHEDULER_STYLE = {
    "smt_wa": dict(color=CATEGORICAL[0], marker="o", label="smt_wa (SMT, exact)"),
    "ls": dict(color=CATEGORICAL[1], marker="s", label="ls (list heuristic)"),
    "dt": dict(color=CATEGORICAL[2], marker="^", label="dt (divisibility heuristic)"),
    "smt_wa+guard": dict(color=CATEGORICAL[3], marker="D", label="smt_wa + 10 µs guard band"),
}


def apply():
    plt.rcParams.update({
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID,
        "axes.labelcolor": TEXT_2,
        "axes.titlecolor": TEXT,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.labelsize": 9.5,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "xtick.color": TEXT_2,
        "ytick.color": TEXT_2,
        "xtick.labelsize": 8.5,
        "ytick.labelsize": 8.5,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "legend.frameon": False,
        "legend.fontsize": 8.5,
        "lines.linewidth": 2,
        "lines.markersize": 6,
        "font.family": "DejaVu Sans",
        "savefig.dpi": 160,
    })
