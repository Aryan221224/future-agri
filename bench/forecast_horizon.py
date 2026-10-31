#!/usr/bin/env python3
"""How many initial captures does a 1-month forecast actually need?

The architecture is: capture the crop once, initialise the twin, then forecast
30 days forward under many candidate interventions and pick the best. The
question this benchmark answers is whether ONE capture is enough to make that
forecast trustworthy, or whether the forecast decays too fast to act on.

Why it might not be. A single observation pins down where the crop *is*. It says
almost nothing about how fast it is *developing* -- and over a 30-day horizon it
is the rate parameters (thermal time to anthesis, leaf lifespan, relative leaf
growth rate) that dominate the error, not the starting state. The model will
drift away from the truth at whatever rate its miscalibrated parameters dictate,
starting from a correct initial condition.

Two or more captures, spaced apart, constrain the *slope* as well as the level,
which is a different and much stronger constraint.

Method: draw a truth with perturbed parameters, run a capture campaign of N
observations over the first `campaign_days`, stop observing, forecast 30 days,
and measure error at the forecast horizon against the truth.
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


def truth_run(weather, factors):
    tw = ZoneTwin(ZoneConfig(zone_id="truth", **BASE), weather, engine=ENGINE)
    pp = tw.model.parameterprovider
    for p, f in factors.items():
        try:
            pp.set_override(p, float(pp[p]) * f, check=False)
        except Exception:
            pass
    tw.run_to_end()
    return tw.output_frame(), float(tw.summary().get("TWSO") or 0.0)


def run_trial(seed, n_obs, members, campaign_days, horizon_days,
              obs_rel_sd, truth_spread, weather):
    rng = np.random.default_rng(seed)
    factors = {p: float(np.exp(rng.normal(0, truth_spread))) for p in PARAMS}
    tdf, truth_yield = truth_run(weather, factors)
    lai = tdf["LAI"].dropna()
    if lai.empty:
        return None

    start = lai.index.min() + dt.timedelta(days=25)      # crop established
    forecast_at = start + dt.timedelta(days=campaign_days)
    target_day = forecast_at + dt.timedelta(days=horizon_days)
    if target_day not in lai.index:
        target_day = lai.index.max()

    # The capture campaign: n_obs observations spread over campaign_days.
    obs = []
    if n_obs > 0:
        if n_obs == 1:
            days = [forecast_at]
        else:
            step = campaign_days / (n_obs - 1)
            days = [start + dt.timedelta(days=int(round(i * step))) for i in range(n_obs)]
        for d in days:
            if d not in lai.index:
                continue
            true_lai = float(lai.loc[d])
            sd = max(0.05, obs_rel_sd * true_lai)
            obs.append(Observation(d, "LAI", float(max(0.01, true_lai + rng.normal(0, sd))), sd))

    pert = ParameterPerturbation(TSUM1=truth_spread, TSUM2=truth_spread,
                                 SPAN=truth_spread, RGRLAI=truth_spread,
                                 TDWI=0.0, WAV=0.15, NAVAILI=0.0)
    ens = EnsembleTwin(ZoneConfig(zone_id="ens", **BASE), weather, n_members=members,
                       perturbation=pert, seed=seed + 7000, engine=ENGINE)
    ens.run(obs)

    # Error at the forecast horizon -- i.e. after observations have STOPPED.
    laisf = []
    for m in ens.members:
        df = m.output_frame()
        if not df.empty and target_day in df.index:
            v = df.loc[target_day, "LAI"]
            if not np.isnan(v):
                laisf.append(float(v))
    if not laisf:
        return None
    pred = float(np.mean(laisf))
    truth_at_horizon = float(lai.loc[target_day])
    mean_y, _, _ = ens.yield_distribution()

    return {
        "n_obs": n_obs,
        "lai_forecast_err": abs(pred - truth_at_horizon),
        "lai_truth": truth_at_horizon,
        "lai_rel_err": abs(pred - truth_at_horizon) / max(truth_at_horizon, 1e-6),
        "yield_err": abs(mean_y - truth_yield),
        "truth_yield": truth_yield,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--members", type=int, default=20)
    ap.add_argument("--campaign-days", type=int, default=21,
                    help="window over which the initial captures are spread")
    ap.add_argument("--horizon-days", type=int, default=30,
                    help="how far the forecast runs after captures stop")
    ap.add_argument("--obs-rel-sd", type=float, default=0.25)
    ap.add_argument("--truth-spread", type=float, default=0.20)
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / "forecast_horizon.json")
    a = ap.parse_args()

    weather = make_synthetic_weather(LAT, LON, dt.date(2025, 6, 1), days=200, seed=7)
    counts = [0, 1, 2, 3, 5]
    rows = {n: [] for n in counts}

    for n in counts:
        for t in range(a.trials):
            r = run_trial(t, n, a.members, a.campaign_days, a.horizon_days,
                          a.obs_rel_sd, a.truth_spread, weather)
            if r:
                rows[n].append(r)
        print(f"  captures={n}: {len(rows[n])} trials done", flush=True)

    print(f"\n{'captures':>9s} {'LAI err @ +' + str(a.horizon_days) + 'd':>18s} "
          f"{'rel err':>9s} {'yield err (kg/ha)':>19s} {'vs 0 captures':>15s}")
    base_lai = statistics.median(r["lai_forecast_err"] for r in rows[0])
    summary = []
    for n in counts:
        if not rows[n]:
            continue
        lai_e = statistics.median(r["lai_forecast_err"] for r in rows[n])
        rel = statistics.median(r["lai_rel_err"] for r in rows[n])
        y_e = statistics.median(r["yield_err"] for r in rows[n])
        gain = 100 * (1 - lai_e / base_lai) if base_lai else 0.0
        summary.append(dict(captures=n, lai_err=lai_e, rel_err=rel, yield_err=y_e,
                            reduction_vs_none_pct=gain))
        print(f"{n:9d} {lai_e:18.3f} {rel:8.1%} {y_e:19.0f} {gain:14.1f}%")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"config": {k: str(v) for k, v in vars(a).items()},
                                 "summary": summary}, indent=2), encoding="utf-8")
    print(f"\nwritten to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
