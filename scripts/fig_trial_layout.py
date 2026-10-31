#!/usr/bin/env python3
"""Fig. 6 — the replicated trial layout (randomised complete block design).

Draws the plot as it will actually be pegged out: 4 treatments x 3 blocks,
randomised within each block with a fixed seed, plus the capture stations.
A uniform patch cannot support a comparative claim; this is what makes the
field trial an experiment rather than a demonstration.

Usage:
    python3 scripts/fig_trial_layout.py [--out poster/fig_trial.png]
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                      # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

PLOT_W, PLOT_H = 5.0, 4.0        # m, one experimental unit = 20 m^2
PATH_W = 1.0                     # m, walkway between plots within a block
BLOCK_GAP = 1.5                  # m, between blocks
N_TREAT, N_BLOCK = 4, 3
SEED = 20260916

TREATMENTS = [
    ("T0", "control\nno N", "#9aa8a1"),
    ("T1", "uniform\n30 kg N/ha", "#2B5FAE"),
    ("T2", "twin plan\nCVaR₈₀", "#0F7B5F"),
    ("T3", "twin plan\nmean-optimal", "#B5461F"),
]

INK, MUTED, LINE = "#16211c", "#5d7268", "#cfd9d4"


def layout() -> list[tuple[int, int, int]]:
    """(block, position, treatment) — randomised within each block."""
    rng = random.Random(SEED)
    out = []
    for b in range(N_BLOCK):
        order = list(range(N_TREAT))
        rng.shuffle(order)
        out += [(b, pos, t) for pos, t in enumerate(order)]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "poster" / "fig_trial.png"))
    ap.add_argument("--dpi", type=int, default=300)
    args = ap.parse_args()

    cells = layout()
    total_w = N_TREAT * PLOT_W + (N_TREAT - 1) * PATH_W
    total_h = N_BLOCK * PLOT_H + (N_BLOCK - 1) * BLOCK_GAP

    fig, ax = plt.subplots(figsize=(6.4, 6.4 * 86.0 / 135.375))

    for b, pos, t in cells:
        code, label, colour = TREATMENTS[t]
        x = pos * (PLOT_W + PATH_W)
        y = (N_BLOCK - 1 - b) * (PLOT_H + BLOCK_GAP)
        ax.add_patch(FancyBboxPatch(
            (x, y), PLOT_W, PLOT_H, boxstyle="round,pad=0,rounding_size=0.22",
            facecolor=colour, alpha=0.17, edgecolor=colour, lw=1.3, zorder=2))
        ax.text(x + PLOT_W / 2, y + PLOT_H * 0.70, code, ha="center", va="center",
                fontsize=11, fontweight="bold", color=colour, zorder=3)
        ax.text(x + PLOT_W / 2, y + PLOT_H * 0.34, label, ha="center", va="center",
                fontsize=6.6, color=MUTED, linespacing=1.25, zorder=3)
        # Two capture stations per plot, clear of the labels.
        for fx, fy in ((0.14, 0.14), (0.86, 0.86)):
            ax.plot(x + PLOT_W * fx, y + PLOT_H * fy, marker="o", ms=3.4,
                    mfc="#fff", mec=INK, mew=0.9, zorder=4)

    for b in range(N_BLOCK):
        y = (N_BLOCK - 1 - b) * (PLOT_H + BLOCK_GAP)
        ax.text(-1.1, y + PLOT_H / 2, f"block {b + 1}", ha="right", va="center",
                fontsize=8, color=MUTED, rotation=90)

    # Scale bar and key, both below the plot so nothing sits over a treatment.
    ax.plot([0, 10], [-2.2, -2.2], color=MUTED, lw=1.1, solid_capstyle="butt")
    for xx in (0, 10):
        ax.plot([xx, xx], [-2.5, -1.9], color=MUTED, lw=1.1)
    ax.text(10.7, -2.2, "10 m", fontsize=7.5, color=MUTED, va="center")

    ax.plot(0.4, -3.9, marker="o", ms=3.4, mfc="#fff", mec=INK, mew=0.9)
    ax.text(1.3, -3.9, "capture station \u2014 2 per plot, 24 in total",
            fontsize=7.5, color=MUTED, va="center")

    ax.text(total_w / 2, total_h + 1.35,
            f"{N_TREAT} treatments \u00d7 {N_BLOCK} blocks \u00b7 randomised within "
            f"block \u00b7 {total_w:.0f} \u00d7 {total_h:.0f} m",
            ha="center", va="center", fontsize=10, color=INK)

    # Fix the drawn extent to the poster slot's 1.574:1 so the figure needs no crop.
    span_y = 22.6
    span_x = span_y * 135.375 / 86.0
    cx, y0, y1 = total_w / 2, -5.4, -5.4 + span_y
    ax.set_xlim(cx - span_x / 2, cx + span_x / 2)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.axis("off")

    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(args.out, dpi=args.dpi, facecolor="white")
    print(f"trial layout -> {N_TREAT}x{N_BLOCK}, {total_w:.0f}x{total_h:.0f} m, "
          f"{len(cells) * 2} stations")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
