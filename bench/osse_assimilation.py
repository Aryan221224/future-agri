#!/usr/bin/env python3
"""Observing System Simulation Experiment: does assimilating UAV LAI help?

This is the experiment that decides whether the twin earns its place. The
architecture claims that flying a field and feeding retrieved LAI into a crop
model produces a better estimate of crop state and end-of-season yield than
running the model open-loop. That claim is testable without a single flight,
and it should be tested before anyone is asked to buy a drone.

Method (standard OSSE):

1. Draw a *truth* trajectory by perturbing crop parameters away from their
   nominal values -- this stands in for "the shipped European/generic cultivar
   parameters are wrong for this field", which is the actual situation.
2. Sample noisy LAI observations from the truth at a realistic flight cadence,
   with a retrieval error that reflects the observation operator rather than
   the radiometer.
3. Run an ensemble open-loop (no observations) and with the EnKF.
4. Compare LAI RMSE over the season and end-of-season yield error, over many
   independent truth draws.

The honest failure mode this is designed to expose: if the ensemble is badly
mis-specified, assimilation can be *worse* than open loop. We report the
distribution across trials, not just the mean, so that shows up.
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
sys.path.insert(0, str(ROOT / "services" / "twin"))
sys.path.insert(0, str(ROOT / "packages" / "fa_core"))

from fa_twin.assimilation import EnsembleTwin, Observation, ParameterPerturbation  # noqa: E402
from fa_twin.engine import ZoneConfig, ZoneTwin  # noqa: E402
from fa_twin.weather import make_synthetic_weather  # noqa: E402

NOMINAL = dict(latitude=30.90, longitude=75.85, crop="rice", variety="Rice_501",
               sowing_date=dt.date(2025, 6, 20), harvest_date=dt.date(2025, 10, 25),
               WAV=35.0)
ENGINE = "Wofost72_WLP_CWB"
PARAMS = ("TSUM1", "TSUM2", "SPAN", "RGRLAI")


def _truth_run(weather, factors: dict[str, float]):
    tw = ZoneTwin(ZoneConfig(zone_id="truth", **NOMINAL), weather, engine=ENGINE)
    pp = tw.model.parameterprovider
    for p, f in factors.items():
        try:
            pp.set_override(p, float(pp[p]) * f, check=False)
        except Exception:
            pass
    tw.run_to_end()
    df = tw.output_frame()
    return df, float(tw.summary().get("TWSO") or 0.0)


def _flight_days(start: dt.date, end: dt.date, every: int) -> list[dt.date]:
    days, d = [], start
    while d <= end:
        days.append(d)
        d += dt.timedelta(days=every)
    return days


def run_trial(seed: int, n_members: int, cadence_days: int, obs_rel_sd: float,
              obs_floor: float, truth_spread: float, weather) -> dict:
    rng = np.random.default_rng(seed)
    truth_factors = {p: float(np.exp(rng.normal(0, truth_spread))) for p in PARAMS}
    truth_df, truth_yield = _truth_run(weather, truth_factors)
    if truth_df.empty:
        return {}

    lai = truth_df["LAI"].dropna()
    first, last = lai.index.min(), lai.index.max()
    fl = [d for d in _flight_days(first + dt.timedelta(days=20), last, cadence_days)
          if d in lai.index]
    # Retrieval error is proportional to the signal, with a floor. A vegetation
    # index recovers LAI with a *relative* error of roughly 20-30%; quoting a
    # single absolute sd makes observations uninformative at low LAI and
    # implausibly precise at high LAI, and either way the OSSE stops measuring
    # what it claims to measure.
    obs = []
    for d in fl:
        truth_lai = float(lai.loc[d])
        sd = max(obs_floor, obs_rel_sd * truth_lai)
        obs.append(Observation(d, "LAI",
                               float(max(0.01, truth_lai + rng.normal(0, sd))), sd))

    cfg = ZoneConfig(zone_id="ens", **NOMINAL)
    pert = ParameterPerturbation(TSUM1=truth_spread, TSUM2=truth_spread,
                                 SPAN=truth_spread, RGRLAI=truth_spread,
                                 TDWI=0.0, WAV=0.15, NAVAILI=0.0)

    def _lai_series(ens):
        rows = []
        for m in ens.members:
            df = m.output_frame()
            if not df.empty:
                rows.append(df["LAI"])
        import pandas as pd
        return pd.concat(rows, axis=1).mean(axis=1) if rows else None

    open_loop = EnsembleTwin(cfg, weather, n_members=n_members, perturbation=pert,
                             seed=seed + 10_000, engine=ENGINE)
    open_loop.run([])
    ol_lai = _lai_series(open_loop)
    ol_mean, ol_sd, _ = open_loop.yield_distribution()

    da = EnsembleTwin(cfg, weather, n_members=n_members, perturbation=pert,
                      seed=seed + 10_000, engine=ENGINE)
    res = da.run(obs)
    da_lai = _lai_series(da)
    da_mean, da_sd, _ = da.yield_distribution()

    def _rmse(series):
        if series is None:
            return float("nan")
        idx = series.index.intersection(lai.index)
        if len(idx) == 0:
            return float("nan")
        return float(np.sqrt(np.mean((series.loc[idx].values - lai.loc[idx].values) ** 2)))

    return {
        "seed": seed,
        "truth_yield": truth_yield,
        "n_obs": len(obs),
        "lai_rmse_open_loop": _rmse(ol_lai),
        "lai_rmse_assimilated": _rmse(da_lai),
        "yield_err_open_loop": abs(ol_mean - truth_yield),
        "yield_err_assimilated": abs(da_mean - truth_yield),
        "yield_sd_open_loop": ol_sd,
        "yield_sd_assimilated": da_sd,
        "rejected_obs": int(sum(res.rejected)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--members", type=int, default=30)
    ap.add_argument("--cadence-days", type=int, default=10,
                    help="days between UAV surveys")
    ap.add_argument("--obs-rel-sd", type=float, default=0.25,
                    help="LAI retrieval error as a fraction of true LAI")
    ap.add_argument("--obs-floor", type=float, default=0.05,
                    help="absolute floor on retrieval sd")
    ap.add_argument("--truth-spread", type=float, default=0.20,
                    help="log-sd of the parameter error the truth is drawn with")
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / "osse.json")
    a = ap.parse_args()

    weather = make_synthetic_weather(NOMINAL["latitude"], NOMINAL["longitude"],
                                     dt.date(2025, 6, 1), days=200, seed=7)
    rows = []
    for i in range(a.trials):
        r = run_trial(i, a.members, a.cadence_days, a.obs_rel_sd, a.obs_floor,
                      a.truth_spread, weather)
        if r:
            rows.append(r)
            print(f"  trial {i:2d}: LAI RMSE {r['lai_rmse_open_loop']:.3f} -> "
                  f"{r['lai_rmse_assimilated']:.3f} | yield err "
                  f"{r['yield_err_open_loop']:7.0f} -> {r['yield_err_assimilated']:7.0f} kg/ha",
                  flush=True)

    def agg(key):
        v = [r[key] for r in rows if not np.isnan(r[key])]
        return dict(mean=statistics.fmean(v), median=statistics.median(v))

    def paired(ol_key, da_key):
        """Paired comparison, which is the only valid one here.

        Each trial is one truth draw evaluated both ways, so the statistic is
        the distribution of per-trial reductions -- not the ratio of the two
        medians. The ratio-of-medians can report a loss while a clear majority
        of paired trials improve, purely because one hard trial dominates the
        open-loop median. That happened on the first run of this benchmark.
        """
        red = [100 * (1 - r[da_key] / r[ol_key]) for r in rows
               if r[ol_key] and not np.isnan(r[ol_key]) and not np.isnan(r[da_key])]
        wins = sum(1 for x in red if x > 0)
        return dict(median_reduction_pct=statistics.median(red),
                    mean_reduction_pct=statistics.fmean(red),
                    improved=f"{wins}/{len(red)}",
                    win_rate=wins / max(len(red), 1))

    lai_ol, lai_da = agg("lai_rmse_open_loop"), agg("lai_rmse_assimilated")
    y_ol, y_da = agg("yield_err_open_loop"), agg("yield_err_assimilated")
    lai_p = paired("lai_rmse_open_loop", "lai_rmse_assimilated")
    y_p = paired("yield_err_open_loop", "yield_err_assimilated")

    summary = {
        "config": vars(a) | {"out": str(a.out)},
        "n_trials": len(rows),
        "lai_rmse": {"open_loop": lai_ol, "assimilated": lai_da, "paired": lai_p},
        "yield_abs_error_kg_ha": {"open_loop": y_ol, "assimilated": y_da, "paired": y_p},
        "rows": rows,
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 68)
    print(f"OSSE: {len(rows)} trials, {a.members} members, survey every {a.cadence_days} d, "
          f"LAI retrieval sd {a.obs_rel_sd:.0%} of signal (floor {a.obs_floor})")
    print("=" * 68)
    print(f"LAI RMSE       median open loop {lai_ol['median']:.3f} -> assimilated {lai_da['median']:.3f}")
    print(f"               paired reduction {lai_p['median_reduction_pct']:+.1f}% median, "
          f"improved in {lai_p['improved']} trials")
    print(f"Yield |error|  median open loop {y_ol['median']:6.0f} -> assimilated {y_da['median']:6.0f} kg/ha")
    print(f"               paired reduction {y_p['median_reduction_pct']:+.1f}% median, "
          f"improved in {y_p['improved']} trials")
    print(f"\nwritten to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
