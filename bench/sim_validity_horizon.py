#!/usr/bin/env python3
"""After a single sensing pass, how long does the simulation stay trustworthy?

The design is: sense once, then run everything in simulation. That is a
legitimate architecture -- but it has one property that must be quantified
rather than assumed, because nothing downstream will warn you about it.

With no further observations, the twin's forecast error grows monotonically
with time since the pass. It never recovers. So there is a horizon beyond which
the simulation's answer stops being good enough to base a decision on, and the
whole value of the approach depends on knowing where that line is.

This measures forecast error against days-since-sensing, for two cases:

  uncalibrated -- imagery only, growth stage not recorded (parameter spread
                  stays near the 0.22 prior). This is what a single pass gives
                  you if the visit collects photos and nothing else.
  staged       -- growth stage recorded and inverted for the development rate
                  (spread ~0.10). Same single visit, one extra measurement.

The gap between the two curves is the value of five minutes per station with a
BBCH scale, expressed in days of usable forecast.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for p in ("services/twin", "packages/fa_core"):
    sys.path.insert(0, str(ROOT / p))

from fa_twin.assimilation import EnsembleTwin, Observation, ParameterPerturbation  # noqa: E402
from fa_twin.engine import ZoneConfig, ZoneTwin  # noqa: E402
from fa_twin.weather import make_synthetic_weather  # noqa: E402

LAT, LON = 30.90, 75.85
ENGINE = "Wofost72_WLP_CWB"
PARAMS = ("TSUM1", "TSUM2", "SPAN", "RGRLAI")
BASE = dict(latitude=LAT, longitude=LON, crop="rice", variety="Rice_501",
            sowing_date=dt.date(2025, 6, 20), harvest_date=dt.date(2025, 10, 25),
            WAV=35.0)
# Relative LAI error above which a forecast should not drive a spend decision.
DECISION_TOLERANCE = 0.20


def truth(weather, factors):
    tw = ZoneTwin(ZoneConfig(zone_id="t", **BASE), weather, engine=ENGINE)
    pp = tw.model.parameterprovider
    for p, f in factors.items():
        try:
            pp.set_override(p, float(pp[p]) * f, check=False)
        except Exception:
            pass
    tw.run_to_end()
    return tw.output_frame()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--members", type=int, default=16)
    ap.add_argument("--sense-on", type=dt.date.fromisoformat, default=dt.date(2025, 7, 20))
    ap.add_argument("--spread-uncalibrated", type=float, default=0.22)
    ap.add_argument("--spread-staged", type=float, default=0.10)
    ap.add_argument("--truth-spread", type=float, default=0.20)
    ap.add_argument("--obs-rel-sd", type=float, default=0.25)
    ap.add_argument("--out", type=Path,
                    default=ROOT / "bench" / "results" / "sim_validity.json")
    a = ap.parse_args()

    weather = make_synthetic_weather(LAT, LON, dt.date(2025, 6, 1), days=200, seed=7)
    horizons = [7, 14, 21, 30, 45, 60]
    out = {"uncalibrated": {h: [] for h in horizons},
           "staged": {h: [] for h in horizons}}

    for t in range(a.trials):
        rng = np.random.default_rng(t)
        factors = {p: float(np.exp(rng.normal(0, a.truth_spread))) for p in PARAMS}
        tdf = truth(weather, factors)
        lai = tdf["LAI"].dropna()
        if a.sense_on not in lai.index:
            continue
        true_now = float(lai.loc[a.sense_on])
        sd = max(0.05, a.obs_rel_sd * true_now)
        obs = [Observation(a.sense_on, "LAI",
                           float(max(0.01, true_now + rng.normal(0, sd))), sd)]

        for case, spread in (("uncalibrated", a.spread_uncalibrated),
                             ("staged", a.spread_staged)):
            pert = ParameterPerturbation(TSUM1=spread, TSUM2=spread, SPAN=spread,
                                         RGRLAI=spread, TDWI=0.0, WAV=0.15, NAVAILI=0.0)
            ens = EnsembleTwin(ZoneConfig(zone_id="e", **BASE), weather,
                               n_members=a.members, perturbation=pert,
                               seed=t + 5000, engine=ENGINE)
            ens.run(obs)
            for h in horizons:
                day = a.sense_on + dt.timedelta(days=h)
                if day not in lai.index:
                    continue
                vals = []
                for m in ens.members:
                    df = m.output_frame()
                    if not df.empty and day in df.index:
                        v = df.loc[day, "LAI"]
                        if not np.isnan(v):
                            vals.append(float(v))
                if not vals:
                    continue
                tv = float(lai.loc[day])
                out[case][h].append(abs(float(np.mean(vals)) - tv) / max(tv, 1e-6))
        print(f"  trial {t} done", flush=True)

    print(f"\nsensing pass on {a.sense_on:%d %b %Y}; decision tolerance "
          f"{DECISION_TOLERANCE:.0%} relative LAI error\n")
    print(f"{'days after':>11s} {'imagery only':>14s} {'+ growth staging':>18s}")
    rows = []
    for h in horizons:
        u = out["uncalibrated"][h]
        s = out["staged"][h]
        if not u or not s:
            continue
        um, sm = statistics.median(u), statistics.median(s)
        rows.append(dict(days=h, uncalibrated=um, staged=sm))
        flag_u = "" if um <= DECISION_TOLERANCE else "  <- past tolerance"
        flag_s = "" if sm <= DECISION_TOLERANCE else "  <- past tolerance"
        print(f"{h:11d} {um:13.1%}{flag_u:20s} {sm:12.1%}{flag_s}")

    def horizon_of(key):
        ok = [r["days"] for r in rows if r[key] <= DECISION_TOLERANCE]
        return max(ok) if ok else 0

    hu, hs = horizon_of("uncalibrated"), horizon_of("staged")
    print(f"\nusable horizon -- imagery only:      {hu} days")
    print(f"usable horizon -- with growth staging: {hs} days")
    if hs > hu:
        print(f"  five minutes per station with a BBCH scale buys "
              f"{hs - hu} extra days of trustworthy simulation")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"tolerance": DECISION_TOLERANCE, "rows": rows,
                                 "horizon_uncalibrated_days": hu,
                                 "horizon_staged_days": hs}, indent=2), encoding="utf-8")
    print(f"\nwritten to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
