#!/usr/bin/env python3
"""WHEN should the three-week campaign run?

The campaign works by constraining the crop's development *rate*, and it can
only do that if the crop visibly changes between the first and last capture by
more than the measurement noise. That gives a hard, checkable design rule:

    signal-to-noise = delta_LAI(campaign window) / (obs_error / sqrt(n_stations))

Run the campaign while the canopy is closing and delta_LAI is 1-3 units, and the
rate is well determined. Run it too early (crop barely emerged) or too late
(canopy closed and LAI flat) and delta_LAI is small, the SNR collapses, the rate
stays unconstrained, and the campaign delivers nothing no matter how carefully
each individual capture was made.

This is the difference between the 0.12 spread where the campaign paid +4520
INR/ha and the 0.18 spread where it bought exactly nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for p in ("services/twin", "packages/fa_core"):
    sys.path.insert(0, str(ROOT / p))

from fa_twin.engine import ZoneConfig, ZoneTwin  # noqa: E402
from fa_twin.weather import make_synthetic_weather  # noqa: E402

LAT, LON = 30.90, 75.85
BASE = dict(latitude=LAT, longitude=LON, crop="rice", variety="Rice_501",
            sowing_date=dt.date(2025, 6, 20), harvest_date=dt.date(2025, 10, 25),
            WAV=35.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--span-days", type=int, default=21)
    ap.add_argument("--stations", type=int, default=11)
    ap.add_argument("--obs-error", type=float, default=0.70,
                    help="per-station LAI retrieval RMSE (UAV literature: 0.6-1.0)")
    ap.add_argument("--min-snr", type=float, default=4.0,
                    help="rate is well constrained above this")
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / "campaign_timing.json")
    a = ap.parse_args()

    weather = make_synthetic_weather(LAT, LON, dt.date(2025, 6, 1), days=200, seed=7)
    tw = ZoneTwin(ZoneConfig(zone_id="t", **BASE), weather, engine="Wofost72_WLP_CWB")
    tw.run_to_end()
    df = tw.output_frame()
    lai = df["LAI"].dropna()
    dvs = df["DVS"].dropna()

    field_error = a.obs_error / np.sqrt(a.stations)
    print(f"per-station LAI error {a.obs_error:.2f}  ->  field-mean error "
          f"{field_error:.3f} with {a.stations} stations")
    print(f"campaign span {a.span_days} d, SNR target >= {a.min_snr:.0f}\n")

    print(f"{'campaign start':>15s} {'LAI start':>10s} {'LAI end':>9s} {'deltaLAI':>9s} "
          f"{'SNR':>7s} {'DVS':>6s}  verdict")
    rows, best = [], None
    days = sorted(lai.index)
    for d0 in days[::5]:
        d1 = d0 + dt.timedelta(days=a.span_days)
        if d1 not in lai.index:
            continue
        l0, l1 = float(lai.loc[d0]), float(lai.loc[d1])
        delta = l1 - l0
        snr = abs(delta) / max(field_error, 1e-9)
        stage = float(dvs.loc[d0]) if d0 in dvs.index else float("nan")
        ok = snr >= a.min_snr
        verdict = "GOOD" if ok else ("weak" if snr >= a.min_snr / 2 else "USELESS")
        rows.append(dict(start=str(d0), lai_start=l0, lai_end=l1, delta=delta,
                         snr=snr, dvs=stage, verdict=verdict))
        if ok and (best is None or snr > best["snr"]):
            best = rows[-1]
        print(f"{d0.strftime('%d %b %Y'):>15s} {l0:10.2f} {l1:9.2f} {delta:9.2f} {snr:7.1f} "
              f"{stage:6.2f}  {verdict}")

    print()
    if best:
        s = dt.date.fromisoformat(best["start"])
        print(f"BEST WINDOW: start {s:%d %b %Y}, finish "
              f"{s + dt.timedelta(days=a.span_days):%d %b %Y}")
        print(f"  LAI moves {best['lai_start']:.2f} -> {best['lai_end']:.2f} "
              f"(delta {best['delta']:+.2f}), SNR {best['snr']:.1f}")
        print(f"  that is {(s - BASE['sowing_date']).days} days after sowing, DVS {best['dvs']:.2f}")
    else:
        print("NO WINDOW CLEARS THE SNR TARGET -- the campaign cannot constrain the")
        print("rate at this observation error. Reduce obs error (better index, more")
        print("careful calibration) or add stations before spending three weeks.")

    good = [r for r in rows if r["verdict"] == "GOOD"]
    if good:
        first = dt.date.fromisoformat(good[0]["start"])
        last = dt.date.fromisoformat(good[-1]["start"])
        print(f"\n  usable start dates span {first:%d %b} to {last:%d %b} "
              f"({(last - first).days} days of latitude)")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"config": {k: str(v) for k, v in vars(a).items()},
                                 "field_mean_error": field_error,
                                 "rows": rows, "best": best}, indent=2), encoding="utf-8")
    print(f"\nwritten to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
