#!/usr/bin/env python3
"""Fig. 7 — the bootstrap capture geometry, drawn to scale.

Side elevation of one station: a phone on a monopod, looking straight down at
the canopy from a fixed height. Every dimension is computed from the camera
model in ``fa_core.geo``, so the drawing cannot drift from the numbers the
retrieval code assumes.

Usage:
    python3 scripts/fig_capture_geometry.py [--out poster/fig_rig.png]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/fa_core"))

import matplotlib                                     # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402
import numpy as np                                    # noqa: E402
from matplotlib.patches import FancyBboxPatch, Polygon, Rectangle  # noqa: E402

from fa_core.geo import CAMERAS                       # noqa: E402

CAM = CAMERAS["phone_main"]
H_AGC = 1.80          # m above the canopy, fixed by the protocol
CANOPY_H = 0.55       # m, canopy top above soil

INK, MUTED, LINE = "#16211c", "#5d7268", "#cfd9d4"
GREEN, BLUE, RUST = "#0F7B5F", "#2B5FAE", "#B5461F"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "poster" / "fig_rig.png"))
    ap.add_argument("--dpi", type=int, default=300)
    args = ap.parse_args()

    gsd = CAM.gsd_m(H_AGC)
    fw, fh = CAM.footprint_m(H_AGC)
    hfov = CAM.hfov_deg()

    fig, ax = plt.subplots(figsize=(6.4, 6.4 * 86.0 / 135.375))

    x_pod, y_cam = 1.50, CANOPY_H + H_AGC

    # Soil and canopy.
    ax.add_patch(Rectangle((-1.0, -0.40), 7.0, 0.40, facecolor="#e6ded2",
                           edgecolor="none", zorder=0))
    rng = np.random.default_rng(7)
    xs = np.linspace(-1.0, 6.0, 900)
    top = CANOPY_H + 0.045 * np.sin(xs * 11.0) + rng.normal(0, 0.012, xs.size)
    ax.fill_between(xs, 0, top, color=GREEN, alpha=0.20, lw=0, zorder=1)
    ax.plot(xs, top, color=GREEN, lw=0.9, alpha=0.75, zorder=1)

    # Field of view.
    half = fw / 2.0
    ax.add_patch(Polygon([(x_pod, y_cam), (x_pod - half, CANOPY_H),
                          (x_pod + half, CANOPY_H)],
                         closed=True, facecolor=BLUE, alpha=0.10,
                         edgecolor=BLUE, lw=1.0, ls=(0, (4, 2.5)), zorder=2))

    # Monopod and phone.
    ax.plot([x_pod, x_pod], [CANOPY_H - 0.06, y_cam], color=INK, lw=2.0,
            solid_capstyle="round", zorder=3)
    ax.add_patch(FancyBboxPatch((x_pod - 0.085, y_cam - 0.015), 0.17, 0.30,
                                boxstyle="round,pad=0,rounding_size=0.03",
                                facecolor="#2a3a32", edgecolor=INK, lw=0.8, zorder=4))
    ax.plot(x_pod, y_cam + 0.035, marker="o", ms=4.2, mfc="#cfd9d4",
            mec="#2a3a32", mew=0.8, zorder=5)
    ax.text(x_pod + 0.16, y_cam + 0.14, "phone, nadir\nmanual exposure + WB locked",
            fontsize=7.2, color=INK, va="center", linespacing=1.3)

    # Height dimension.
    xd = x_pod - half - 0.32
    ax.annotate("", (xd, CANOPY_H), (xd, y_cam),
                arrowprops=dict(arrowstyle="<->", color=MUTED, lw=1.0))
    ax.text(xd - 0.08, CANOPY_H + H_AGC / 2, f"{H_AGC:.2f} m\nabove canopy",
            fontsize=7.4, color=MUTED, ha="right", va="center", linespacing=1.3)

    # Footprint dimension.
    yd = CANOPY_H - 0.30
    ax.annotate("", (x_pod - half, yd), (x_pod + half, yd),
                arrowprops=dict(arrowstyle="<->", color=BLUE, lw=1.0))
    ax.text(x_pod, yd - 0.12, f"{fw:.2f} m footprint", fontsize=7.4,
            color=BLUE, ha="center", va="top")

    # Grey card and stake.
    ax.add_patch(Rectangle((x_pod + 0.34, CANOPY_H - 0.015), 0.34, 0.035,
                           facecolor="#b8b8b8", edgecolor=INK, lw=0.6, zorder=4))
    ax.text(x_pod + 0.51, CANOPY_H + 0.10, "grey card", fontsize=6.9,
            color=MUTED, ha="center")
    ax.plot([x_pod - 0.72, x_pod - 0.72], [CANOPY_H - 0.10, CANOPY_H + 0.34],
            color=RUST, lw=1.6, solid_capstyle="round", zorder=4)
    ax.add_patch(Rectangle((x_pod - 0.80, CANOPY_H + 0.30), 0.17, 0.13,
                           facecolor="#fff", edgecolor=RUST, lw=0.9, zorder=5))
    ax.text(x_pod - 0.715, CANOPY_H + 0.365, "S7", fontsize=6.2, color=RUST,
            ha="center", va="center", fontweight="bold", zorder=6)

    # Numbers panel.
    px = 3.00
    ax.add_patch(FancyBboxPatch((px, 0.16), 1.52, 1.66,
                                boxstyle="round,pad=0.06,rounding_size=0.06",
                                facecolor="#f5f8f6", edgecolor=LINE, lw=1.0, zorder=3))
    rows = [
        ("sensor", CAM.name.split(" (")[0]),
        ("frame", f"{CAM.image_width_px} × {CAM.image_height_px} px"),
        ("HFOV", f"{hfov:.0f}°"),
        ("GSD", f"{gsd * 1000:.2f} mm/px"),
        ("footprint", f"{fw:.2f} × {fh:.2f} m"),
        ("per frame", f"{fw * fh:.1f} m²"),
        ("kit cost", "< ₹10,000"),
    ]
    for i, (k, v) in enumerate(rows):
        y = 1.64 - i * 0.208
        ax.text(px + 0.10, y, k, fontsize=6.9, color=MUTED, va="center")
        ax.text(px + 1.42, y, v, fontsize=6.9, color=INK, va="center",
                ha="right", fontweight="bold")

    ax.text(px + 0.76, 1.94, "one station, six frames",
            fontsize=8.4, color=INK, ha="center", va="center")
    ax.text(px + 0.76, 0.00,
            "panel · nadir · oblique\nmacro · context · panel",
            fontsize=6.2, color=MUTED, ha="center", va="center", linespacing=1.35)

    span_x = 5.66
    span_y = span_x * 86.0 / 135.375
    ax.set_xlim(-0.92, -0.92 + span_x)
    ax.set_ylim(-0.44, -0.44 + span_y)
    ax.set_aspect("equal")
    ax.axis("off")

    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(args.out, dpi=args.dpi, facecolor="white")
    print(f"capture geometry -> GSD {gsd*1000:.2f} mm/px, "
          f"footprint {fw:.2f} x {fh:.2f} m, HFOV {hfov:.0f} deg")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
