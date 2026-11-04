"""v5: stem reserves to the grain under drought, canopy size in the satellite anchor, Sentinel-2.

    python ml/mead/v5.py fetch-sat      # Landsat 2014-2021 + Sentinel-2 2017-2021 per pivot -> data/sat_fresh.json
    python ml/mead/v5.py run-2001       # the 15 AmeriFlux site-years           -> results/mead/runs_v5.json
    python ml/mead/v5.py run-fresh      # 12 combine-yield seasons 2014-2021    -> results/mead/fresh.json

What v5 changes, all fixed before any test season was scored:

* ``remobilisation`` (fa_twin.remobilisation): CERES-Maize's rule -- when drought leaves the
  grain short of its demand, living stem down to 85 % of its silking weight moves to the grain.
  No parameter fitted. No effect without water stress, so the irrigated calibration and every
  potential-production run are unchanged.
* the satellite anchor (fa_farm.anchor.SATELLITE): canopy SIZE fitted as well as timing, the
  satellite-cover operator k = 0.33 fitted on the three training seasons only (Ne1 2001-2003,
  Landsat vs destructive LAI), parameters only (no state overwrite).
* imagery: Sentinel-2 (5-day revisit) for 2017 onwards, Landsat before.

The 2014-2021 seasons were not used for anything before this run: not to build v4, not to
choose v5. They are the cleanest test in this study. Irrigated pivots run water-limited with
the farm's metered passes (ml/mead/water.py); the rainfed pivot with its recorded sowing date,
seeding rate and hybrid (AmeriFlux BADM).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in ("packages/fa_core", "services/twin", "services/farm", "services/planner"):
    sys.path.insert(0, str(ROOT / p))
sys.path.insert(0, str(Path(__file__).parent))
warnings.filterwarnings("ignore")

import study as S  # noqa: E402
import water as W  # noqa: E402

RAINFED = {   # AmeriFlux BADM US-Ne3: planting (seeds/acre, hybrid GDU) and combine yield (bu/acre at 15.5 %)
    ("Ne3", 2015): dict(sowing="04-30", seeds_ac=26000, gdu=2730, yield_bu=186.0, residue=0.50, events=[]),
    ("Ne3", 2017): dict(sowing="05-08", seeds_ac=28500, gdu=2750, yield_bu=192.0, residue=0.50, events=[]),
    ("Ne3", 2021): dict(sowing="04-29", seeds_ac=28000, gdu=2750, yield_bu=189.5, residue=0.50, events=[]),
}
FRESH = {**{k: v for k, v in W.SEASONS.items() if v["yield_bu"]}, **RAINFED}
SAT = S.DATA / "sat_fresh.json"
CUTOFF = (7, 15)
ENGINE = "Wofost72_WLP_CWB"


def fetch_sat() -> None:
    from fa_farm.sources.base import Cache
    from fa_farm.sources.satellite import s2_cover_series
    cache = Cache(S.DATA / "cache")
    out = json.loads(SAT.read_text()) if SAT.exists() else {}
    for (site, y) in FRESH:
        key = f"{site}_{y}"
        lat, lon, _ = S.SITES[site]
        poly = S.pivot_polygon(lat, lon)
        rec = out.get(key, {})
        a, b = dt.date(y, 4, 1), dt.date(y, 11, 15)
        if "Landsat" not in rec:
            rec["Landsat"] = [(d.isoformat(), n, c) for d, n, c in S.landsat_cover_series(poly, a, b)]
        if y >= 2017 and "S2" not in rec:
            series, _ = s2_cover_series(poly, a, b, cache)
            rec["S2"] = [(d.isoformat(), n, c) for d, n, c in series]
        out[key] = rec
        SAT.write_text(json.dumps(out))
        print(key, {k: len(v) for k, v in rec.items()}, flush=True)


def _sat(site, y, sensor):
    d = json.loads(SAT.read_text())[f"{site}_{y}"].get(sensor, [])
    return [(dt.date.fromisoformat(a), b, c) for a, b, c in d]


# -- 2001-2007: the same 15 site-years as v1-v4 --------------------------------------------------
def run_2001(resume: bool = False) -> dict:
    from fa_farm.anchor import SATELLITE, anchor_to_cover
    cal = json.loads((S.OUT / "hybrid_calibration_v4.json").read_text())["params"]
    theta = [cal[k] for k in S.CAL_PARAMS_V4]
    p = S.OUT / "runs_v5.json"
    res = json.loads(p.read_text()) if resume and p.exists() else {}
    for site in S.SITES:
        engine = "Wofost72_PP" if S.SITES[site][2] else ENGINE
        for y in S.MAIZE[site]:
            if f"{site}_{y}" in res:
                continue
            sci = S.science_inputs_v4(site, y)
            fwd = S._sim_v4(site, y, theta, sci, remobilisation=True)
            out = {"train": (site, y) in S.TRAIN, "forward": {k: v for k, v in fwd.items() if k != "cfg"}}
            wx = [r for r in sci["weather"] if dt.date(y - 1, 11, 1) <= r["DAY"] <= dt.date(y, 12, 31)]
            series = S._hist(site)["landsat"][y]
            for tag, until in (("anchored", None), ("midseason", dt.date(y, *CUTOFF))):
                use = [x for x in series if until is None or x[0] <= until]
                ov, info = anchor_to_cover(fwd["cfg"], wx, use, members=24, seed=0, engine=engine,
                                           soil_evap_factor=sci["management"]["soil_evap_factor"], **SATELLITE)
                r = S._sim_v4(site, y, theta, sci, extra=ov, remobilisation=True)
                out[tag] = {k: v for k, v in r.items() if k != "cfg"}
                out[tag].update(n_obs=info.get("n_obs"), leaf_share=info.get("leaf_share"))
            res[f"{site}_{y}"] = out
            print("v5", site, y, "TRAIN" if out["train"] else "test ", " ".join(
                f"{k} {out[k]['yield_t_ha']:.1f}" for k in ("forward", "midseason", "anchored")), flush=True)
            p.write_text(json.dumps(res, default=str))
    return res


# -- 2014-2021: fresh seasons, combine yields -----------------------------------------------------
def _events(site, y):
    return [{"date": dt.date.fromisoformat(f"{y}-{d}"), "irrigation_cm": a * W.IN, "efficiency": W.EFFICIENCY}
            for d, a, _ in FRESH[(site, y)]["events"]] or None


def _yield(site, y, st, events, remob, extra=None):
    st2 = {**st, "overrides": {**st["overrides"], **(extra or {})}}
    tw = W._twin(site, y, st2, ENGINE, events=events, remobilisation=remob)
    tw.run_to_end()
    df = tw.output_frame()
    last = df.iloc[-1]
    return {"yield_t_ha": round(float(last["TWSO"]) / 1000 / (1 - S.MOIST), 2),
            "agb_t_ha": round(float(last["TAGP"]) / 1000, 2), "lai_max": round(float(df["LAI"].max()), 2),
            "remobilised_kg_ha": tw.summary().get("remobilised_kg_ha")}, tw.cfg


def fresh_one(key) -> dict:
    from fa_farm.anchor import SATELLITE, anchor_to_cover
    site, y = key
    s = FRESH[key]
    st = W.setup(site, y, s)
    ev = _events(site, y)
    t0 = time.perf_counter()
    out = {"site": site, "year": y, "irrigated": S.SITES[site][2], "measured_t_ha": round(s["yield_bu"] * W.BU, 2),
           "sensor": "S2" if y >= 2017 else "Landsat"}
    runs = [("v4", False, {}, out["sensor"]), ("v5", True, SATELLITE, out["sensor"])]
    if y >= 2017:
        runs.append(("v5_landsat", True, SATELLITE, "Landsat"))
    for name, remob, kw, sensor in runs:
        fwd, cfg = _yield(site, y, st, ev, remob)
        r = {"forward": fwd}
        series = _sat(site, y, sensor)
        for tag, until in (("midseason", dt.date(y, *CUTOFF)), ("anchored", None)):
            use = [x for x in series if until is None or x[0] <= until]
            ov, info = anchor_to_cover(cfg, st["weather"], use, members=24, seed=0, engine=ENGINE,
                                       soil_evap_factor=st["evap_factor"], events=ev, **kw)
            r[tag], _ = _yield(site, y, st, ev, remob, extra=ov)
            r[tag].update(n_obs=info.get("n_obs"), leaf_share=info.get("leaf_share"))
        out[name] = r
    out["seconds"] = round(time.perf_counter() - t0, 1)
    print(site, y, out["measured_t_ha"], " | ".join(
        f"{n}: " + " ".join(f"{m[:3]} {out[n][m]['yield_t_ha']:.2f}" for m in ("forward", "midseason", "anchored"))
        for n in ("v4", "v5", "v5_landsat") if n in out), f"| {out['seconds']} s", flush=True)
    return out


def _rmse(e):
    return math.sqrt(sum(x * x for x in e) / len(e)) if e else None


def summarise(rows) -> dict:
    summ = {}
    groups = {"all": lambda r: True, "irrigated": lambda r: r["irrigated"], "rainfed": lambda r: not r["irrigated"]}
    for name in ("v4", "v5", "v5_landsat"):
        for mode in ("forward", "midseason", "anchored"):
            for g, f in groups.items():
                e = [r[name][mode]["yield_t_ha"] - r["measured_t_ha"] for r in rows if name in r and f(r)]
                if e:
                    summ.setdefault(name, {}).setdefault(mode, {})[g] = {
                        "n": len(e), "rmse": _rmse(e), "bias": sum(e) / len(e)}
    # Sentinel-2 vs Landsat on the same 2017-2021 seasons
    both = [r for r in rows if "v5_landsat" in r]
    summ["s2_vs_landsat_2017_2021"] = {
        sensor: {mode: _rmse([r[name][mode]["yield_t_ha"] - r["measured_t_ha"] for r in both])
                 for mode in ("midseason", "anchored")}
        for sensor, name in (("S2", "v5"), ("Landsat", "v5_landsat"))}
    summ["scenes_2017_2021"] = {
        "S2": sum(r["v5"]["anchored"]["n_obs"] or 0 for r in both) / max(1, len(both)),
        "Landsat": sum(r["v5_landsat"]["anchored"]["n_obs"] or 0 for r in both) / max(1, len(both))}
    base = sum(r["measured_t_ha"] for r in rows) / len(rows)
    summ["naive_mean_of_these_seasons"] = {"note": "uses the test seasons' own mean: a lower bound no forecast has",
                                           "rmse": _rmse([base - r["measured_t_ha"] for r in rows])}
    return summ


def run_fresh() -> None:
    from multiprocessing import Pool
    keys = list(FRESH)
    with Pool(2) as pool:
        rows = pool.map(fresh_one, keys, chunksize=1)
    out = {"seasons": rows, "summary": summarise(rows)}
    (S.OUT / "fresh.json").write_text(json.dumps(out, indent=1, default=str))
    for name, v in out["summary"].items():
        print(name, json.dumps(v, default=lambda x: round(x, 2))[:400])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch-sat":
        fetch_sat()
    elif cmd == "run-2001":
        run_2001(resume="--resume" in sys.argv)
    elif cmd == "run-fresh":
        run_fresh()
    else:
        print(__doc__)
