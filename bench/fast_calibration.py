#!/usr/bin/env python3
"""Can anything replace the three-week campaign?

The campaign exists to constrain the crop's development RATE, and a rate needs
time separation. So any faster route must substitute for that separation. Four
candidates, tested here against the same requirement (get the growth-rate
parameter spread below ~0.15, the threshold at which the planner stops being
paralysed and the intervention becomes justifiable):

  1. PHENOLOGY STAGING -- one visit. Observe growth stage (BBCH/DVS) and combine
     it with accumulated thermal time since sowing, which weather data already
     gives you. Because DVS ~= TSUM_accumulated / TSUM1 before anthesis,
     observing DVS *directly inverts* for TSUM1. No time separation needed at
     all: the time separation already happened, between sowing and today, and
     the weather record measured it for you.

  2. COMPRESSED CAMPAIGN -- more visits, shorter window. Quantifies the penalty.

  3. SPACE-FOR-TIME -- sample plots at different growth stages simultaneously
     (staggered sowing dates), reading a growth curve across space in one visit.

  4. PRIOR SEASON -- last year's fitted parameters as this year's prior.

The scaling law that governs (2) is worth stating up front, because it is what
makes compressing the window expensive: for a rate estimated by regression over
n points spanning T days, SE(rate) ~ sigma / (sqrt(n) * T). Halving T therefore
costs a FOURFOLD increase in observations to break even.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "twin"))

THRESHOLD = 0.15     # parameter spread below which the planner will act


def rate_se(sigma: float, n_obs: int, span_days: float) -> float:
    """Standard error of a linear rate fit -- the scaling law, made concrete.

    For n points evenly spaced over T days, SD(t) = T/sqrt(12) * sqrt((n+1)/(n-1)),
    and SE(slope) = sigma / (SD(t) * sqrt(n)).
    """
    if n_obs < 2 or span_days <= 0:
        return float("inf")
    sd_t = (span_days / math.sqrt(12)) * math.sqrt((n_obs + 1) / (n_obs - 1))
    return sigma / (sd_t * math.sqrt(n_obs))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prior-spread", type=float, default=0.22)
    ap.add_argument("--stations", type=int, default=11)
    ap.add_argument("--lai-rmse", type=float, default=0.70)
    ap.add_argument("--trials", type=int, default=20000)
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / "fast_calibration.json")
    a = ap.parse_args()
    rng = np.random.default_rng(0)
    field_sigma = a.lai_rmse / math.sqrt(a.stations)
    results = []

    print(f"prior parameter spread {a.prior_spread:.2f}; target < {THRESHOLD:.2f}")
    print(f"{a.stations} stations, per-station LAI RMSE {a.lai_rmse:.2f} "
          f"-> field-mean sigma {field_sigma:.3f}\n")

    # ---- 1. Phenology staging, one visit ---------------------------------
    print("1. PHENOLOGY STAGING (one visit)")
    print("   DVS ~= TSUM_acc / TSUM1, and TSUM_acc comes free from the weather")
    print("   record -- so observing growth stage inverts directly for TSUM1.\n")
    print(f"   {'staging error':>15s} {'observed at DVS':>16s} {'TSUM1 spread':>13s}  verdict")
    for dvs_err in (0.02, 0.05, 0.10, 0.15):
        for dvs_at in (0.4, 0.7):
            # TSUM1 = TSUM_acc / DVS ; relative error propagates from DVS.
            post = dvs_err / dvs_at
            combined = 1.0 / math.sqrt(1.0 / a.prior_spread**2 + 1.0 / max(post, 1e-6) ** 2)
            ok = combined < THRESHOLD
            results.append(dict(method="phenology", dvs_err=dvs_err, dvs_at=dvs_at,
                                spread=combined, days=1, clears=ok))
            print(f"   {dvs_err:15.2f} {dvs_at:16.2f} {combined:13.3f}  "
                  f"{'CLEARS' if ok else 'no'}")

    # ---- 2. Compressed campaign -----------------------------------------
    print("\n2. COMPRESSED CAMPAIGN (trade window length against visit count)")
    print(f"   {'visits':>7s} {'span (d)':>9s} {'SE(rate)':>10s} {'equiv spread':>13s}  verdict")
    # Calibrate the mapping from SE(rate) to parameter spread using the known
    # anchor: 3 visits over 21 days is measured to reach ~0.12.
    anchor = rate_se(field_sigma, 3, 21.0)
    k = 0.12 / anchor
    for n_v, span in ((3, 21), (3, 14), (3, 10), (5, 10), (8, 10), (5, 7), (12, 7), (20, 5)):
        se = rate_se(field_sigma, n_v, span)
        spread = k * se
        ok = spread < THRESHOLD
        results.append(dict(method="campaign", visits=n_v, span=span,
                            spread=spread, days=span, clears=ok))
        print(f"   {n_v:7d} {span:9d} {se:10.4f} {spread:13.3f}  "
              f"{'CLEARS' if ok else 'no':6s} {'  <- baseline' if (n_v,span)==(3,21) else ''}")

    # ---- 3. Space-for-time ----------------------------------------------
    print("\n3. SPACE-FOR-TIME (plots at different growth stages, one visit)")
    print("   Equivalent to a campaign whose 'span' is the sowing-date spread,")
    print("   but every point carries extra between-plot variance.")
    print(f"   {'sowing spread':>14s} {'plots':>6s} {'extra CV':>9s} {'equiv spread':>13s}  verdict")
    for sow_spread, plots, extra_cv in ((14, 3, 0.15), (21, 3, 0.15), (21, 5, 0.15),
                                        (21, 5, 0.30), (30, 4, 0.20)):
        sigma_eff = math.sqrt(field_sigma**2 + (extra_cv * 2.5) ** 2)
        spread = k * rate_se(sigma_eff, plots, sow_spread)
        ok = spread < THRESHOLD
        results.append(dict(method="space_for_time", sow_spread=sow_spread, plots=plots,
                            extra_cv=extra_cv, spread=spread, days=1, clears=ok))
        print(f"   {sow_spread:12d} d {plots:6d} {extra_cv:9.2f} {spread:13.3f}  "
              f"{'CLEARS' if ok else 'no'}")

    # ---- 4. Prior season -------------------------------------------------
    print("\n4. PRIOR SEASON (last year's fit as this year's prior)")
    print(f"   {'last-yr spread':>15s} {'yr-to-yr drift':>15s} {'this-yr prior':>14s}  verdict")
    for last, drift in ((0.12, 0.08), (0.12, 0.12), (0.10, 0.06), (0.15, 0.10)):
        prior = math.sqrt(last**2 + drift**2)
        ok = prior < THRESHOLD
        results.append(dict(method="prior_season", last=last, drift=drift,
                            spread=prior, days=0, clears=ok))
        print(f"   {last:15.2f} {drift:15.2f} {prior:14.3f}  {'CLEARS' if ok else 'no'}")

    print("\n" + "=" * 70)
    fastest = sorted([r for r in results if r["clears"]], key=lambda r: r["days"])
    if fastest:
        f = fastest[0]
        print(f"FASTEST ROUTE THAT CLEARS THE THRESHOLD: {f['method']} "
              f"({f['days']} day(s) of field time, spread {f['spread']:.3f})")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"threshold": THRESHOLD, "results": results}, indent=2), encoding="utf-8")
    print(f"written to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
