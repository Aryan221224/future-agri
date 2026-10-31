#!/usr/bin/env python3
"""Regenerate the poster's mission-plan figure (Fig. 9).

Plans coverage over the demo field, partitions the swaths into
energy-feasible sorties, re-verifies the result independently of the solver,
and draws it. Nothing is hand-placed: if the planner changes, the figure
changes with it.

Usage:
    python3 scripts/fig_mission.py [--out poster/fig_mission.png]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/planner"):
    sys.path.insert(0, str(ROOT / p))

import matplotlib                                      # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                        # noqa: E402
from shapely.geometry import Polygon                   # noqa: E402

from fa_planner.coverage import plan_coverage          # noqa: E402
from fa_planner.routing import DroneModel, plan_sorties, verify  # noqa: E402

# Same demo field as `make demo` and the dashboard: 700 x 420 m, 29.4 ha.
WIDTH_M, HEIGHT_M = 700.0, 420.0
SWATH_W = 26.0
HOME = (0.0, 0.0)

SORTIE_COLOURS = ["#0F7B5F", "#2B5FAE", "#B5461F", "#6B4E9E"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "poster" / "fig_mission.png"))
    ap.add_argument("--dpi", type=int, default=300)
    args = ap.parse_args()

    field = Polygon([(0, 0), (WIDTH_M, 0), (WIDTH_M, HEIGHT_M), (0, HEIGHT_M)])
    cov = plan_coverage(field, swath_width_m=SWATH_W, headland_passes=0)
    plan = plan_sorties(cov.swaths, home=HOME, drone=DroneModel())

    problems = verify(plan)
    if problems:
        print("VERIFY FAILED:", *problems, sep="\n  ")
        return 1

    fig, ax = plt.subplots(figsize=(6.15, 4.55))
    xs, ys = field.exterior.xy
    ax.fill(xs, ys, color="#0F7B5F", alpha=0.06, zorder=0)
    ax.plot(xs, ys, color="#5d7268", lw=1.1, zorder=1)

    for s in plan.sorties:
        c = SORTIE_COLOURS[s.index % len(SORTIE_COLOURS)]
        for sw in s.swaths:
            x, y = sw.line.xy
            ax.plot(x, y, color=c, lw=1.6, solid_capstyle="round", zorder=2)
        ax.plot([], [], color=c, lw=2.4,
                label=f"sortie {s.index} · {s.energy_wh:.0f} Wh · "
                      f"{s.duration_s/60:.1f} min")

    ax.plot(*HOME, marker="^", ms=9, color="#16211c", zorder=4)
    ax.annotate("home / battery swap", HOME, textcoords="offset points",
                xytext=(9, -2), fontsize=8, color="#16211c", va="center")

    area_ha = field.area / 10_000.0
    ax.set_title(f"{area_ha:.1f} ha · {len(cov.swaths)} swaths · "
                 f"{plan.n_sorties} energy-feasible sorties",
                 fontsize=9.5, color="#16211c", pad=9)
    ax.set_xlabel("metres east", fontsize=8, color="#5d7268")
    ax.set_ylabel("metres north", fontsize=8, color="#5d7268")
    ax.tick_params(labelsize=7.5, colors="#5d7268")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#cfd9d4")
    ax.set_aspect("equal")
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.30), ncol=plan.n_sorties,
              frameon=False, fontsize=8, labelcolor="#16211c")

    fig.tight_layout()
    fig.savefig(args.out, dpi=args.dpi, facecolor="white",
                bbox_inches="tight", pad_inches=0.14)

    print(f"mission figure -> {plan.n_sorties} sorties, "
          f"{plan.total_time_s/60:.1f} min, verified")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
