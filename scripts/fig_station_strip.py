#!/usr/bin/env python3
"""Fig. 3 inset — what one station yields, as a letterbox strip.

Six frames per station, in order. The two reference-panel shots bracket the
others so a cloud passing mid-capture is caught rather than silently absorbed
into the radiometry. Steps are read from ``CAPTURE_PROTOCOL`` so the strip and
the field sheet cannot disagree.

Usage:
    python3 scripts/fig_station_strip.py [--out poster/fig_strip.png]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/gateway"))

import matplotlib                                      # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                        # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch, Polygon, Rectangle  # noqa: E402

from fa_gateway.campaign import CAPTURE_PROTOCOL       # noqa: E402

INK, MUTED, LINE = "#16211c", "#5d7268", "#cfd9d4"
GREEN, BLUE, RUST, GREY = "#0F7B5F", "#2B5FAE", "#B5461F", "#9aa8a1"

# Display names and the accent each step carries. Keyed on the protocol's own
# step ids, so a step added there shows up here as a missing key rather than
# silently dropping out of the figure.
STEPS = {
    "panel_before":  ("reference\npanel", GREY),
    "nadir_canopy":  ("nadir\ncanopy", BLUE),
    "oblique_row":   ("45° oblique\nalong row", GREEN),
    "closeup_leaf":  ("macro\nleaf", RUST),
    "wide_context":  ("wide\ncontext", MUTED),
    "panel_after":   ("reference\npanel", GREY),
}

RATIO = 176.6 / 21.0


def canopy(ax, cx, y0, w, colour):
    xs = [cx - w / 2 + w * t / 20 for t in range(21)]
    ys = [y0 + (0.018 if t % 2 else 0.0) for t in range(21)]
    ax.plot(xs, ys, color=colour, lw=0.9, zorder=3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "poster" / "fig_strip.png"))
    ap.add_argument("--dpi", type=int, default=400)
    args = ap.parse_args()

    steps = [s for s, _ in CAPTURE_PROTOCOL]
    missing = [s for s in steps if s not in STEPS]
    if missing:
        print("protocol steps not drawn:", missing)
        return 1

    fig, ax = plt.subplots(figsize=(7.2, 7.2 / RATIO))
    n = len(steps)
    cw = RATIO / n

    for i, sid in enumerate(steps):
        label, colour = STEPS[sid]
        cx = (i + 0.5) * cw

        ax.add_patch(FancyBboxPatch(
            (cx - 0.31, 0.44), 0.62, 0.40,
            boxstyle="round,pad=0,rounding_size=0.05",
            facecolor=colour, alpha=0.13, edgecolor=colour, lw=0.9, zorder=2))

        if sid.startswith("panel"):
            ax.add_patch(Rectangle((cx - 0.13, 0.55), 0.26, 0.18,
                                   facecolor="#cfd4d1", edgecolor=colour,
                                   lw=0.8, zorder=3))
        elif sid == "nadir_canopy":
            canopy(ax, cx, 0.53, 0.44, colour)
            ax.annotate("", (cx, 0.565), (cx, 0.79),
                        arrowprops=dict(arrowstyle="-|>", color=colour, lw=1.0))
        elif sid == "oblique_row":
            canopy(ax, cx, 0.53, 0.44, colour)
            ax.annotate("", (cx + 0.05, 0.565), (cx - 0.17, 0.79),
                        arrowprops=dict(arrowstyle="-|>", color=colour, lw=1.0))
        elif sid == "closeup_leaf":
            ax.add_patch(Polygon([(cx - 0.14, 0.58), (cx, 0.78), (cx + 0.14, 0.58)],
                                 closed=True, facecolor=colour, alpha=0.30,
                                 edgecolor=colour, lw=0.8, zorder=3))
            ax.add_patch(Circle((cx + 0.07, 0.65), 0.065, facecolor="none",
                                edgecolor=colour, lw=1.0, zorder=4))
        else:  # wide_context
            ax.add_patch(Rectangle((cx - 0.22, 0.56), 0.44, 0.17,
                                   facecolor="none", edgecolor=colour,
                                   lw=0.8, zorder=3))
            ax.plot([cx + 0.13, cx + 0.13], [0.56, 0.78], color=RUST,
                    lw=1.0, zorder=4)

        ax.text(cx, 0.235, label, ha="center", va="center", fontsize=6.6,
                color=INK, linespacing=1.3)
        ax.text(cx - 0.355, 0.795, str(i + 1), ha="center", va="center",
                fontsize=5.6, color=MUTED, fontweight="bold")

        if i < n - 1:
            ax.annotate("", ((i + 1) * cw + 0.055, 0.64),
                        ((i + 1) * cw - 0.055, 0.64),
                        arrowprops=dict(arrowstyle="-|>", color=LINE, lw=1.0))

    ax.text(RATIO / 2, 0.045,
            "one station · six frames · panel shots bracket the set, "
            "so a cloud mid-capture is caught and not absorbed into the radiometry",
            ha="center", va="center", fontsize=5.6, color=MUTED)

    ax.set_xlim(0, RATIO)
    ax.set_ylim(0, 1)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(args.out, dpi=args.dpi, facecolor="white")
    print(f"station strip -> {n} steps, ratio {RATIO:.2f}:1")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
