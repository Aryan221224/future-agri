"""One camera pass: what does it buy, with the fixed filter, on realistic weather?

Replaces the claim the poster made from ``osse_assimilation.py`` (16 weekly
observations on 102 mm/yr weather). Setup, ported from the 30 Oct audit (S2):

* site Bennett University, potato Potato_701, planted 25 Oct, harvest 10 Feb;
* truth = one weather realisation + parameter factors drawn at 20 % (log-sd)
  for TSUM1, TSUM2, SPAN, RGRLAI, TDWI;
* ensemble = the same prior spread; each member's weather is the observed past
  up to the pass, a 15-day forecast after it, then climatology (forecast
  ensemble, ``fa_twin.weather.forecast_ensemble``);
* one pass at day ``--pass-day``: canopy cover (sd 0.04) and growth stage
  (sd 0.05 DVS), assimilated through their operators with the fixed EnKF
  (parameters written back into the running crop).

Reported per variant: LAI RMSE over the 30 days after the pass (ensemble mean
vs truth), yield absolute error, how often the 80 % interval holds the truth,
and the paired count of trials where yield beat open loop.

    python bench/one_pass_osse.py --trials 40 --pass-day 30 45 --inflation 1.0 1.3
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
import warnings
from multiprocessing import Pool
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/twin"):
    sys.path.insert(0, str(ROOT / p))
warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

from fa_twin.assimilation import EnsembleTwin, Observation, ParameterPerturbation  # noqa: E402
from fa_twin.engine import ZoneConfig, ZoneTwin  # noqa: E402
from fa_twin.weather import forecast_ensemble, generate_records, records_provider  # noqa: E402

LAT, LON = 28.45, 77.58
PLANT, HARVEST = dt.date(2025, 10, 25), dt.date(2026, 2, 10)
WSTART = dt.date(2025, 10, 1)
SPREAD = 0.20
PNAMES = ("TSUM1", "TSUM2", "SPAN", "RGRLAI", "TDWI")
K = 0.6
ENGINE = "Wofost72_WLP_CWB"


def cfg():
    return ZoneConfig(zone_id="osse", latitude=LAT, longitude=LON, crop="potato",
                      variety="Potato_701", sowing_date=PLANT, harvest_date=HARVEST)


def truth_run(factors: dict, weather):
    tw = ZoneTwin(cfg(), weather, engine=ENGINE)
    pp = tw.model.parameterprovider
    for p, f in factors.items():
        pp.set_override(p, float(pp[p]) * f, check=False)
    tw.run_to_end()
    df = tw.output_frame()
    return df, float(tw.summary().get("TWSO") or 0.0)


def lai_series(ens: EnsembleTwin):
    frames = [m.output_frame()["LAI"].fillna(0.0) for m in ens.members]
    return sum(frames) / len(frames)


def metrics(ens, truth_df, truth_y, pass_date):
    mean_lai = lai_series(ens)
    window = [d for d in truth_df.index if pass_date < d <= pass_date + dt.timedelta(days=30)]
    err = [(mean_lai.get(d, 0.0) - (truth_df.loc[d, "LAI"] or 0.0)) for d in window]
    _, _, ys = ens.yield_distribution()
    lo, hi = np.percentile(ys, [10, 90])
    lo_os, hi_os, _ = ens.interval(0.8)
    return dict(lai_rmse_30d=float(np.sqrt(np.mean(np.square(err)))),
                yield_abs_err=float(abs(ys.mean() - truth_y)),
                in_80=bool(lo <= truth_y <= hi),
                in_80_os=bool(lo_os <= truth_y <= hi_os),
                yield_rel_spread=float(ys.std() / max(ys.mean(), 1.0)))


def trial(args):
    t, pass_day, inflations, members = args
    rng = np.random.default_rng(1000 + t)
    f_true = {p: float(np.exp(rng.normal(-0.5 * SPREAD ** 2, SPREAD))) for p in PNAMES}
    recs = generate_records(WSTART, 200, 5000 + t)
    w_true = records_provider(recs, LAT, LON)
    truth_df, truth_y = truth_run(f_true, w_true)
    pass_date = PLANT + dt.timedelta(days=pass_day)
    lai_t = float(truth_df.loc[pass_date, "LAI"] or 0.0)
    dvs_t = float(truth_df.loc[pass_date, "DVS"] or 0.0)
    if lai_t <= 0:
        return None
    obs_cc = float(np.clip(1 - np.exp(-K * lai_t) + 0.04 * rng.normal(), 0.01, 0.985))
    obs_dvs = float(dvs_t + 0.05 * rng.normal())

    ens_recs = forecast_ensemble(recs, pass_date, members, seed=9000 + t)
    ws = [records_provider(r, LAT, LON) for r in ens_recs]
    pert = ParameterPerturbation(TSUM1=SPREAD, TSUM2=SPREAD, SPAN=SPREAD, RGRLAI=SPREAD,
                                 TDWI=SPREAD, WAV=0.0, NAVAILI=0.0)

    def ensemble(lam):
        return EnsembleTwin(cfg(), ws[0], n_members=members, perturbation=pert,
                            seed=20_000 + t, engine=ENGINE, inflation=lam, weathers=ws)

    out = {}
    ol = ensemble(1.0)
    ol.run([])
    out["open_loop"] = metrics(ol, truth_df, truth_y, pass_date)
    for lam in inflations:
        for name, obs in (("cover", [Observation(pass_date, "CC", obs_cc, 0.04)]),
                          ("cover_stage", [Observation(pass_date, "CC", obs_cc, 0.04),
                                           Observation(pass_date, "DVS", obs_dvs, 0.05)])):
            e = ensemble(lam)
            e.run(obs)
            out[f"{name}@{lam:g}"] = metrics(e, truth_df, truth_y, pass_date)
    out["meta"] = dict(trial=t, lai_true=lai_t, dvs_true=dvs_t)
    return out


def summarise(rows):
    rows = [r for r in rows if r]
    keys = [k for k in rows[0] if k != "meta"]
    base = np.array([r["open_loop"]["yield_abs_err"] for r in rows])
    base_l = np.array([r["open_loop"]["lai_rmse_30d"] for r in rows])
    out = {}
    for k in keys:
        y = np.array([r[k]["yield_abs_err"] for r in rows])
        l = np.array([r[k]["lai_rmse_30d"] for r in rows])
        cov = np.mean([r[k]["in_80"] for r in rows])
        cov_os = np.mean([r[k].get("in_80_os", False) for r in rows])
        se = float(np.sqrt(cov * (1 - cov) / len(rows)))
        out[k] = dict(
            lai_rmse_30d=round(float(l.mean()), 3),
            lai_rmse_change_pct=round(float(100 * (l.mean() / base_l.mean() - 1)), 1),
            lai_better_than_open_loop=f"{int((l < base_l).sum())}/{len(rows)}",
            yield_abs_err_mean=round(float(y.mean()), 0),
            yield_better_than_open_loop=f"{int((y < base).sum())}/{len(rows)}",
            interval80_coverage_percentile=round(float(cov), 2),
            interval80_coverage_order_stat=round(float(cov_os), 2), coverage_se=round(se, 2),
            n=len(rows))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--members", type=int, default=24)
    ap.add_argument("--pass-day", type=int, nargs="+", default=[30, 45])
    ap.add_argument("--inflation", type=float, nargs="+", default=[1.0, 1.3])
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / "one_pass_osse.json"))
    a = ap.parse_args()
    t0 = time.time()
    res = {"setup": dict(site="Bennett 28.45N 77.58E", crop="potato Potato_701",
                         weather="normals-calibrated generator + 15-day forecast ensemble",
                         prior_spread=SPREAD, members=a.members, trials=a.trials,
                         obs="canopy cover sd 0.04; growth stage sd 0.05 DVS")}
    with Pool(a.workers) as pool:
        for pd in a.pass_day:
            rows = pool.map(trial, [(t, pd, a.inflation, a.members) for t in range(a.trials)])
            res[f"pass_day_{pd}"] = summarise(rows)
            print(f"pass day {pd}:", json.dumps(res[f"pass_day_{pd}"], indent=1), flush=True)
    res["runtime_s"] = round(time.time() - t0, 1)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(res, open(a.out, "w"), indent=1)
    print("->", a.out)


if __name__ == "__main__":
    main()
