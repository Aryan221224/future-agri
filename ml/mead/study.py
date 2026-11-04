"""Mead, Nebraska validation study: the Future Agri twin against a real, measured farm.

US-Ne1 (University of Nebraska, irrigated continuous maize, centre pivot, AmeriFlux). Everything
the simulation sees is real: the field (its pivot circle), SoilGrids soil, NASA POWER daily
weather of the actual season, the field's own Sentinel-2 cover. Three ways of predicting the
season, scored against measurements none of them used (AmeriFlux BADM, published yields):

  forward    real soil + weather, shipped maize parameters, no fitting (a generic crop model)
  anchored   the twin as it runs in the software: parameters fitted to this season's Sentinel-2
  midseason  the same, but only Sentinel-2 up to a cut-off date (what the software knew then)

    python ml/mead/study.py fetch --years 2017-2021        # real data -> ml/mead/data/
    python ml/mead/study.py run   --years 2017-2021        # simulations -> ml/results/mead/
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in ("packages/fa_core", "services/twin", "services/farm", "services/scan", "services/drone",
          "services/runner", "services/planner", "services/loop"):
    sys.path.insert(0, str(ROOT / p))

from shapely.geometry import Point, Polygon  # noqa: E402

DATA = ROOT / "ml" / "mead" / "data"
OUT = ROOT / "ml" / "results" / "mead"
NE1 = (41.1651, -96.4766)            # AmeriFlux US-Ne1 tower (pivot centre), elevation 361 m
PIVOT_R_M = 395.0                    # ~49 ha centre-pivot circle


def pivot_polygon(lat=NE1[0], lon=NE1[1], r=PIVOT_R_M, n=72) -> Polygon:
    m_lat = 111_320.0
    m_lon = 111_320.0 * math.cos(math.radians(lat))
    return Polygon([(lon + r * math.cos(2 * math.pi * k / n) / m_lon, lat + r * math.sin(2 * math.pi * k / n) / m_lat)
                    for k in range(n)])


def season_window(year: int, sowing: dt.date | None = None, harvest: dt.date | None = None):
    return sowing or dt.date(year, 5, 1), harvest or dt.date(year, 10, 15)


def fetch(years: list[int]) -> None:
    from fa_farm.sources.base import Cache
    from fa_farm.sources.satellite import s2_cover_series
    from fa_farm.sources.soil import soilgrids_profile
    from fa_farm.sources.weather import power_daily
    DATA.mkdir(parents=True, exist_ok=True)
    cache = Cache(DATA / "cache")
    poly = pivot_polygon()
    profile, soil, prov = soilgrids_profile(*NE1, cache)
    (DATA / "soil.json").write_text(json.dumps({"profile": profile, "soil": soil.as_dict(),
                                                "provenance": prov.as_dict()}, default=str, indent=1))
    print("soil", soil.as_dict())
    for y in years:
        sow, harv = season_window(y)
        recs, p, elev = power_daily(*NE1, dt.date(y, 1, 1), dt.date(y, 12, 31), cache)
        series, sp = s2_cover_series(poly, dt.date(y, 4, 1), dt.date(y, 11, 15), cache)
        out = {"year": y, "elevation_m": elev, "weather": [{**r, "DAY": r["DAY"].isoformat()} for r in recs],
               "s2": [(d.isoformat(), ndvi, cov) for d, ndvi, cov in series],
               "provenance": {"weather": p.as_dict(), "s2": sp.as_dict()}}
        (DATA / f"season_{y}.json").write_text(json.dumps(out, default=str))
        rain = sum(r["RAIN"] for r in recs if sow <= r["DAY"] <= harv)
        print(y, f"weather {len(recs)} days, May-Oct rain {rain:.0f} mm, S2 {len(series)} clear scenes, "
                 f"peak cover {max((c for _, _, c in series), default=float('nan')):.2f}")


def _load(y: int) -> dict:
    d = json.loads((DATA / f"season_{y}.json").read_text())
    for r in d["weather"]:
        r["DAY"] = dt.date.fromisoformat(r["DAY"])
    d["s2"] = [(dt.date.fromisoformat(a), b, c) for a, b, c in d["s2"]]
    return d


def _soil():
    from fa_twin.engine import SoilParams
    s = json.loads((DATA / "soil.json").read_text())["soil"]
    return SoilParams(**s)


def simulate(y: int, variety: str, sowing: dt.date, harvest: dt.date, overrides: dict | None = None,
             irrigated: bool = True) -> dict:
    from fa_twin.engine import ZoneConfig, ZoneTwin
    from fa_twin.weather import records_provider
    d = _load(y)
    cfg = ZoneConfig("ne1", *NE1, "maize", variety, sowing, harvest, soil=_soil(),
                     param_overrides=dict(overrides or {}))
    wx = records_provider(d["weather"], *NE1, d["elevation_m"], "NASA POWER")
    tw = ZoneTwin(cfg, wx, engine="Wofost72_PP" if irrigated else "Wofost72_WLP_CWB")
    tw.run_to_end()
    df = tw.output_frame()
    return {"daily": {str(k): {c: (None if v != v else float(v)) for c, v in row.items()
                               if c in ("DVS", "LAI", "TAGP", "TWSO")} for k, row in df.iterrows()},
            "summary": {k: v for k, v in tw.summary().items() if isinstance(v, (int, float, str))}}


def anchored(y: int, variety: str, sowing: dt.date, harvest: dt.date, until: dt.date | None = None,
             irrigated: bool = True, seed: int = 0) -> tuple[dict, dict]:
    from fa_farm.anchor import anchor_to_cover
    from fa_twin.engine import ZoneConfig
    d = _load(y)
    series = [s for s in d["s2"] if until is None or s[0] <= until]
    cfg = ZoneConfig("ne1", *NE1, "maize", variety, sowing, harvest, soil=_soil())
    return anchor_to_cover(cfg, d["weather"], series, members=24, seed=seed,
                           engine="Wofost72_PP" if irrigated else "Wofost72_WLP_CWB")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "fetch-historic", "truth", "run", "run-v2", "calibrate", "run-v3",
                                       "calibrate-v4", "run-v4"])
    ap.add_argument("--resume", action="store_true", help="run-v4: keep site-years already in runs_v4.json")
    ap.add_argument("--years", default="2017-2021")
    a = ap.parse_args(argv)
    lo, _, hi = a.years.partition("-")
    years = list(range(int(lo), int(hi or lo) + 1))
    if a.cmd == "fetch":
        fetch(years)
    elif a.cmd == "fetch-historic":
        fetch_historic()
    elif a.cmd == "truth":
        for site, v in ground_truth().items():
            for y, r in v.items():
                print(site, y, {k: (round(x, 2) if isinstance(x, float) else len(x)) for k, x in r.items()})
    elif a.cmd == "run":
        run_all()
    elif a.cmd == "run-v2":
        run_all_v2()
    elif a.cmd == "calibrate":
        calibrate_hybrid()
    elif a.cmd == "run-v3":
        run_all_v3()
    elif a.cmd == "calibrate-v4":
        calibrate_v4()
    elif a.cmd == "run-v4":
        run_all_v4(resume=a.resume)
    return 0



# ---------------------------------------------------------------- Landsat 5/7 for 2001-2007
SITES = {"Ne1": (41.16506, -96.47664, True), "Ne2": (41.16487, -96.47008, True), "Ne3": (41.17967, -96.43965, False)}
LANDSAT_CLEAR = (5440, 21824)        # QA_PIXEL: clear land (Landsat 4-7, Landsat 8-9)


def landsat_cover_series(polygon_wgs84, start: dt.date, end: dt.date, max_cloud: float = 60.0) -> list[tuple]:
    """[(date, field median NDVI, FVC)] from Landsat Collection 2 L2 (Planetary Computer), the
    same server-side statistics and NDVI->cover as the software's Sentinel-2 path. Surface
    reflectance = DN x 2.75e-5 - 0.2, so NDVI = (N - R) / (N + R - 14545.45) in DN."""
    from pystac_client import Client
    from fa_farm.sources.base import session
    from fa_farm.sources.satellite import MPC_STAC, MPC_STATS, fvc, median_from_histogram
    items = list(Client.open(MPC_STAC).search(
        collections=["landsat-c2-l2"], intersects=polygon_wgs84.__geo_interface__,
        datetime=f"{start.isoformat()}/{end.isoformat()}", query={"eo:cloud_cover": {"lt": max_cloud},
                                                                  "platform": {"in": ["landsat-5", "landsat-7"]}},
        max_items=200).items())
    clear = "|".join(f"(qa_pixel=={v})" for v in LANDSAT_CLEAR)
    expr = f"where(({clear})&(red>7273)&(nir08>7273),(nir08-red)/(nir08+red-14545.45),-2)"
    feat = {"type": "Feature", "geometry": polygon_wgs84.__geo_interface__, "properties": {}}
    out = {}
    for it in sorted(items, key=lambda i: i.datetime):
        try:
            r = session().post(MPC_STATS, params={"collection": "landsat-c2-l2", "item": it.id, "expression": expr,
                                                  "asset_as_band": "true", "histogram_bins": 500,
                                                  "histogram_range": "-1,1", "max_size": 1024}, json=feat, timeout=60)
            r.raise_for_status()
            band = next(iter(r.json()["properties"]["statistics"].values()))
            ndvi = median_from_histogram(band)[0]
        except Exception as e:  # noqa: BLE001 -- one bad scene is skipped
            print("  skip", it.id, type(e).__name__, str(e)[:80])
            continue
        if ndvi is not None:
            d = it.datetime.date()
            if d not in out:                       # L5 and L7 on the same day: keep the first
                out[d] = (d, ndvi, fvc(ndvi))
    return [out[d] for d in sorted(out)]


# ---------------------------------------------------------------- ground truth (AmeriFlux BADM)
C_GRAIN, C_BIOMASS, MOIST = 0.45, 0.439, 0.155   # grain C fraction; BADM's 43.9 % C for biomass; 15.5 % grain moisture
MAIZE = {"Ne1": range(2001, 2008), "Ne2": (2001, 2003, 2005, 2007), "Ne3": (2001, 2003, 2005, 2007)}


def ground_truth() -> dict:
    """{site: {year: {"yield_t_ha" (15.5 %), "agb_t_ha", "lai": [(date, LAI)]}}} for maize seasons."""
    import pandas as pd
    out = {}
    for site in SITES:
        df = pd.read_excel(next((DATA / "badm").glob(f"AMF_US-{site}_BIF_*.xlsx")))

        def grp(g):
            s = df[df.VARIABLE_GROUP == g]
            return s.pivot_table(index="GROUP_ID", columns="VARIABLE", values="DATAVALUE", aggfunc="first")
        prod, lai = grp("GRP_AG_PROD_CROP"), grp("GRP_LAI")
        site_out = {}
        for y in MAIZE[site]:
            rows = prod[prod.AG_PROD_DATE_START.astype(str).str[:4] == str(y)]
            v = {r.AG_PROD_CROP_ORGAN: float(r.AG_PROD_CROP) for r in rows.itertuples()}
            grain_c = v.get("Fruits", v["Total"] - v["Foliage"] if {"Total", "Foliage"} <= set(v) else None)
            rec = {"lai": sorted((dt.datetime.strptime(str(r.LAI_DATE)[:8], "%Y%m%d").date(), float(r.LAI_TOT))
                                 for r in lai.itertuples() if str(r.LAI_DATE)[:4] == str(y))}
            if grain_c is not None:
                rec["yield_t_ha"] = grain_c / C_GRAIN / 100 / (1 - MOIST)
            if "Total" in v:
                rec["agb_t_ha"] = v["Total"] / C_BIOMASS / 100
            site_out[y] = rec
        out[site] = site_out
    return out


# ---------------------------------------------------------------- real inputs 2001-2007
def fetch_historic() -> None:
    from fa_farm.sources.base import Cache
    from fa_farm.sources.soil import soilgrids_profile
    from fa_farm.sources.weather import power_daily
    cache = Cache(DATA / "cache")
    for site, (lat, lon, _) in SITES.items():
        profile, soil, prov = soilgrids_profile(lat, lon, cache)
        recs, p, elev = power_daily(lat, lon, dt.date(2001, 1, 1), dt.date(2007, 12, 31), cache)
        poly = pivot_polygon(lat, lon)
        sat = {}
        for y in MAIZE[site]:
            sat[y] = [(d.isoformat(), n, c) for d, n, c in landsat_cover_series(poly, dt.date(y, 4, 1),
                                                                              dt.date(y, 11, 15))]
            print(site, y, f"{len(sat[y])} clear Landsat views", flush=True)
        (DATA / f"hist_{site}.json").write_text(json.dumps({
            "site": site, "lat": lat, "lon": lon, "elevation_m": elev, "soil": soil.as_dict(),
            "weather": [{**r, "DAY": r["DAY"].isoformat()} for r in recs], "landsat": sat,
            "provenance": {"soil": prov.as_dict(), "weather": p.as_dict()}}, default=str))


def _hist(site: str) -> dict:
    d = json.loads((DATA / f"hist_{site}.json").read_text())
    for r in d["weather"]:
        r["DAY"] = dt.date.fromisoformat(r["DAY"])
    d["landsat"] = {int(y): [(dt.date.fromisoformat(a), b, c) for a, b, c in v] for y, v in d["landsat"].items()}
    return d


def run_site_year(site: str, y: int, variety: str = "Grain_maize_204", cutoff=(7, 15), seed: int = 0) -> dict:
    """forward / anchored / midseason for one maize season -> daily LAI, yield, AGB."""
    from fa_farm.anchor import anchor_to_cover
    from fa_twin.engine import SoilParams, ZoneConfig, ZoneTwin
    from fa_twin.weather import records_provider
    d = _hist(site)
    lat, lon, irrigated = SITES[site]
    sow, harv = dt.date(y, 5, 1), dt.date(y, 10, 15)
    engine = "Wofost72_PP" if irrigated else "Wofost72_WLP_CWB"
    soil = SoilParams(**d["soil"])
    wx_recs = [r for r in d["weather"] if dt.date(y - 1, 11, 1) <= r["DAY"] <= dt.date(y, 12, 31)]

    def sim(overrides):
        cfg = ZoneConfig(site, lat, lon, "maize", variety, sow, harv, soil=soil, param_overrides=dict(overrides))
        tw = ZoneTwin(cfg, records_provider(wx_recs, lat, lon, d["elevation_m"], "NASA POWER"), engine=engine)
        tw.run_to_end()
        df = tw.output_frame()
        lai = {k.isoformat() if hasattr(k, "isoformat") else str(k): (None if row["LAI"] != row["LAI"] else float(row["LAI"]))
               for k, row in df.iterrows()}
        last = df.iloc[-1]
        return {"lai": lai, "yield_t_ha": float(last["TWSO"]) / 1000 / (1 - MOIST),
                "agb_t_ha": float(last["TAGP"]) / 1000, "dvs_end": float(last["DVS"])}
    out = {"forward": sim({})}
    series = d["landsat"][y]
    base = ZoneConfig(site, lat, lon, "maize", variety, sow, harv, soil=soil)
    for tag, until in (("anchored", None), ("midseason", dt.date(y, *cutoff))):
        use = [s for s in series if until is None or s[0] <= until]
        ov, info = anchor_to_cover(base, wx_recs, use, members=24, seed=seed, engine=engine)
        r = sim(ov)
        r.update(overrides=ov, n_obs=info.get("n_obs"), note=info.get("note"))
        out[tag] = r
    out["landsat"] = [(s[0].isoformat(), s[1], s[2]) for s in series]
    return out


def run_all() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    res = {}
    for site in SITES:
        for y in MAIZE[site]:
            r = run_site_year(site, y)
            res[f"{site}_{y}"] = r
            print(site, y, " ".join(f"{k} {r[k]['yield_t_ha']:.1f}" for k in ("forward", "anchored", "midseason")),
                  flush=True)
            (OUT / "runs.json").write_text(json.dumps(res, default=str))
    return res



# ---------------------------------------------------------------- v2: the updated science
HYBRID_GDU = 2750.0      # ~113 CRM: the hybrids Mead plants (site records 2014-2021: 111-115 CRM, 2730-2875 GDU)


def science_inputs(site: str, y: int, variety: str = "Grain_maize_204") -> dict:
    """What the updated software derives before the season, from data available before it:
    0-150 cm root-zone soil, deep maize rooting, hybrid thermal time from 1984-2000 weather,
    soil water at sowing from that winter's weather."""
    from fa_farm.sources.base import Cache
    from fa_farm.sources.soil import soilgrids_rootzone
    from fa_twin.hybrids import crop_table, rooting_override, tsums_for_hybrid
    from fa_twin.soilwater import available_water_at_sowing
    lat, lon, _ = SITES[site]
    cache = Cache(DATA / "cache")
    _, sp, _ = soilgrids_rootzone(lat, lon, cache)
    crop = crop_table("maize", variety)
    hist = json.loads((DATA / "history_1984_2000.json").read_text())
    for r in hist:
        r["DAY"] = dt.date.fromisoformat(r["DAY"])
    ph = tsums_for_hybrid(hist, "05-01", HYBRID_GDU, crop)
    d = _hist(site)
    wx = hist + d["weather"]
    wav = available_water_at_sowing(wx, sp.SMFCF, sp.SMW, dt.date(y, 5, 1), sp.RDMSOL, lat=lat,
                                    elev=d["elevation_m"])
    return {"soil": sp, "overrides": {**ph.overrides(), **rooting_override("maize", sp.RDMSOL, crop["RDMCR"])},
            "WAV": wav["WAV_cm"], "wav_info": wav, "weather": wx}


def run_site_year_v2(site: str, y: int, variety: str = "Grain_maize_204", cutoff=(7, 15), seed: int = 0) -> dict:
    from fa_farm.anchor import anchor_to_cover
    from fa_twin.engine import ZoneConfig, ZoneTwin
    from fa_twin.weather import records_provider
    d = _hist(site)
    lat, lon, irrigated = SITES[site]
    sci = science_inputs(site, y, variety)
    sow, harv = dt.date(y, 5, 1), dt.date(y, 10, 15)
    engine = "Wofost72_PP" if irrigated else "Wofost72_WLP_CWB"
    wx_recs = [r for r in sci["weather"] if dt.date(y - 1, 11, 1) <= r["DAY"] <= dt.date(y, 12, 31)]

    def cfg(extra):
        return ZoneConfig(site, lat, lon, "maize", variety, sow, harv, soil=sci["soil"], WAV=sci["WAV"],
                          param_overrides={**sci["overrides"], **extra})

    def sim(extra):
        tw = ZoneTwin(cfg(extra), records_provider(wx_recs, lat, lon, d["elevation_m"], "NASA POWER"), engine=engine)
        tw.run_to_end()
        df = tw.output_frame()
        lai = {k.isoformat() if hasattr(k, "isoformat") else str(k): (None if row["LAI"] != row["LAI"] else float(row["LAI"]))
               for k, row in df.iterrows()}
        last = df.iloc[-1]
        return {"lai": lai, "yield_t_ha": float(last["TWSO"]) / 1000 / (1 - MOIST),
                "agb_t_ha": float(last["TAGP"]) / 1000, "dvs_end": float(last["DVS"])}
    out = {"forward": sim({}), "science": {"overrides": sci["overrides"], "WAV": sci["WAV"], "wav": sci["wav_info"],
                                           "soil": sci["soil"].as_dict()}}
    series = d["landsat"][y]
    for tag, until in (("anchored", None), ("midseason", dt.date(y, *cutoff))):
        use = [s for s in series if until is None or s[0] <= until]
        ov, info = anchor_to_cover(cfg({}), wx_recs, use, members=24, seed=seed, engine=engine)
        r = sim(ov)
        r.update(overrides=ov, n_obs=info.get("n_obs"))
        out[tag] = r
    return out


def run_all_v2() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    res = {}
    for site in SITES:
        for y in MAIZE[site]:
            r = run_site_year_v2(site, y)
            res[f"{site}_{y}"] = r
            print("v2", site, y, f"WAV {r['science']['WAV']:.1f} cm", " ".join(
                f"{k} {r[k]['yield_t_ha']:.1f}" for k in ("forward", "anchored", "midseason")), flush=True)
            (OUT / "runs_v2.json").write_text(json.dumps(res, default=str))
    return res



# ---------------------------------------------------------------- v3: a regional hybrid, calibrated on TRAINING seasons only
TRAIN = [("Ne1", 2001), ("Ne1", 2002), ("Ne1", 2003)]
CAL_PARAMS = ("SPAN", "RGRLAI", "TDWI", "SOW_DELAY")      # leaf life, early leaf growth, initial weight, sowing offset (d)
CAL_START = (33.0, 0.0294, 50.0, 0.0)
CAL_BOUNDS = ((25.0, 70.0), (0.010, 0.050), (20.0, 120.0), (0.0, 25.0))


def _sim_v3(site, y, theta, sci=None, engine=None, extra=None):
    from fa_twin.engine import ZoneConfig, ZoneTwin
    from fa_twin.weather import records_provider
    d = _hist(site)
    lat, lon, irrigated = SITES[site]
    sci = sci or science_inputs(site, y)
    span, rgr, tdwi, delay = theta
    sow, harv = dt.date(y, 5, 1) + dt.timedelta(days=int(round(delay))), dt.date(y, 10, 15)
    wx = [r for r in sci["weather"] if dt.date(y - 1, 11, 1) <= r["DAY"] <= dt.date(y, 12, 31)]
    ov = {**sci["overrides"], "SPAN": span, "RGRLAI": rgr, "TDWI": tdwi, **(extra or {})}
    cfg = ZoneConfig(site, lat, lon, "maize", "Grain_maize_204", sow, harv, soil=sci["soil"], WAV=sci["WAV"],
                     param_overrides=ov)
    tw = ZoneTwin(cfg, records_provider(wx, lat, lon, d["elevation_m"], "NASA POWER"),
                  engine=engine or ("Wofost72_PP" if irrigated else "Wofost72_WLP_CWB"))
    tw.run_to_end()
    df = tw.output_frame()
    lai = {k.isoformat() if hasattr(k, "isoformat") else str(k): (0.0 if row["LAI"] != row["LAI"] else float(row["LAI"]))
           for k, row in df.iterrows()}
    last = df.iloc[-1]
    return {"lai": lai, "yield_t_ha": float(last["TWSO"]) / 1000 / (1 - MOIST), "agb_t_ha": float(last["TAGP"]) / 1000,
            "dvs_end": float(last["DVS"]), "cfg": cfg, "overrides": ov}


def _loss(theta, cases, gt):
    """Normalised errors on LAI through the season, total biomass and grain yield."""
    err = 0.0
    for (site, y), sci in cases:
        r = _sim_v3(site, y, theta, sci)
        t = gt[site][y]
        lai_e = [(r["lai"].get(d.isoformat(), 0.0) - v) / 1.0 for d, v in t["lai"]]
        err += sum(e * e for e in lai_e) / len(lai_e)
        err += ((r["agb_t_ha"] - t["agb_t_ha"]) / 2.0) ** 2 + ((r["yield_t_ha"] - t["yield_t_ha"]) / 1.0) ** 2
    return err


def calibrate_hybrid() -> dict:
    import numpy as np
    from scipy.optimize import minimize
    gt = ground_truth()
    cases = [((s, y), science_inputs(s, y)) for s, y in TRAIN]
    lo, hi = np.array([b[0] for b in CAL_BOUNDS]), np.array([b[1] for b in CAL_BOUNDS])
    f = lambda z: _loss(lo + (hi - lo) / (1 + np.exp(-z)), cases, gt)          # noqa: E731
    z0 = np.log((np.array(CAL_START) - lo + 1e-6) / (hi - np.array(CAL_START) + 1e-6))
    best = None
    for start in (z0, np.zeros(4)):
        r = minimize(f, start, method="Nelder-Mead", options={"maxiter": 260, "xatol": 1e-3, "fatol": 1e-3})
        if best is None or r.fun < best.fun:
            best = r
    theta = lo + (hi - lo) / (1 + np.exp(-best.x))
    out = {"params": dict(zip(CAL_PARAMS, map(float, theta))), "loss": float(best.fun),
           "start_loss": float(f(z0)), "train": TRAIN, "evals": int(best.nfev)}
    (OUT / "hybrid_calibration.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))
    return out



def run_all_v3(cutoff=(7, 15), seed: int = 0) -> dict:
    from fa_farm.anchor import anchor_to_cover
    cal = json.loads((OUT / "hybrid_calibration.json").read_text())["params"]
    theta = [cal[k] for k in CAL_PARAMS]
    res = {}
    for site in SITES:
        lat, lon, irrigated = SITES[site]
        engine = "Wofost72_PP" if irrigated else "Wofost72_WLP_CWB"
        for y in MAIZE[site]:
            sci = science_inputs(site, y)
            fwd = _sim_v3(site, y, theta, sci)
            out = {"train": (site, y) in TRAIN, "forward": {k: v for k, v in fwd.items() if k not in ("cfg",)}}
            wx = [r for r in sci["weather"] if dt.date(y - 1, 11, 1) <= r["DAY"] <= dt.date(y, 12, 31)]
            series = _hist(site)["landsat"][y]
            for tag, until in (("anchored", None), ("midseason", dt.date(y, *cutoff))):
                use = [x for x in series if until is None or x[0] <= until]
                ov, info = anchor_to_cover(fwd["cfg"], wx, use, members=24, seed=seed, engine=engine)
                r = _sim_v3(site, y, theta, sci, extra=ov)
                out[tag] = {k: v for k, v in r.items() if k not in ("cfg",)}
                out[tag]["n_obs"] = info.get("n_obs")
            res[f"{site}_{y}"] = out
            print("v3", site, y, "TRAIN" if out["train"] else "test ", " ".join(
                f"{k} {out[k]['yield_t_ha']:.1f}" for k in ("forward", "anchored", "midseason")), flush=True)
            (OUT / "runs_v3.json").write_text(json.dumps(res, default=str))
    return res



# ---------------------------------------------------------------- v4: measured management, local weather, residue, population
# Sowing date and plant population: UNL Carbon Sequestration Program site records (csp.unl.edu/Public/sites.htm)
# for 2001-2003; later years are not published there, so each field's documented mean is used and flagged.
# Residue cover: BADM site descriptions (all three fields disked just before the 2001 planting; no-till since,
# except Ne1 conservation-plowed after each harvest from 2005 leaving ~1/3 of the residue) with the cover
# figures in fa_twin.management (Eck & Brown 2004).
MANAGEMENT = {
    ("Ne1", 2001): ("05-10", 82000), ("Ne1", 2002): ("05-09", 81000), ("Ne1", 2003): ("05-15", 77000),
    ("Ne2", 2001): ("05-11", 81000), ("Ne2", 2003): ("05-14", 78000),
    ("Ne3", 2001): ("05-14", 53000), ("Ne3", 2003): ("05-13", 58000),
}
FIELD_DEFAULT = {"Ne1": ("05-11", 80000), "Ne2": ("05-12", 79500), "Ne3": ("05-13", 55500)}
CAL_PARAMS_V4 = ("SPAN", "RGRLAI", "TDWI", "TSUMEM")       # TDWI at the 80 000 plants/ha reference
CAL_START_V4 = (33.0, 0.0294, 50.0, 110.0)
CAL_BOUNDS_V4 = ((25.0, 70.0), (0.010, 0.050), (20.0, 120.0), (50.0, 250.0))
RRI_MAIZE = 3.1          # cm/day after V5: Ordonez et al. 2018, Field Crops Res. 215:122-131


def management(site: str, y: int) -> dict:
    from fa_twin.management import soil_evaporation_factor
    documented = (site, y) in MANAGEMENT
    mmdd, pop = MANAGEMENT.get((site, y), FIELD_DEFAULT[site])
    if y == 2001:
        cover, how = 0.10, "disked before planting"
    elif site == "Ne1":
        cover, how = (0.70, "no-till after maize") if y <= 2005 else (0.30, "conservation plow, ~1/3 residue left")
    else:
        cover, how = 0.50, "no-till after soybean"
    return {"sowing": dt.date.fromisoformat(f"{y}-{mmdd}"), "population": pop, "documented": documented,
            "residue_cover": cover, "residue": how, "soil_evap_factor": soil_evaporation_factor(cover)}


_V4_WEATHER: dict = {}


def _weather_v4(site: str) -> tuple[list, dict]:
    """POWER 1984-2007 with the MEAD 6 S gauge's temperature and rain wherever it reported them."""
    if site not in _V4_WEATHER:
        from fa_farm.sources.base import Cache
        from fa_farm.sources.stations import merge_station
        lat, lon, _ = SITES[site]
        hist = json.loads((DATA / "history_1984_2000.json").read_text())
        for r in hist:
            r["DAY"] = dt.date.fromisoformat(r["DAY"])
        wx = hist + _hist(site)["weather"]
        recs, prov, info = merge_station(wx, lat, lon, dt.date(1984, 1, 1), dt.date(2007, 12, 31), Cache(DATA / "cache"))
        _V4_WEATHER[site] = (recs, {"provenance": prov.as_dict() if prov else None, **info})
    return _V4_WEATHER[site]


def science_inputs_v4(site: str, y: int, variety: str = "Grain_maize_204") -> dict:
    from fa_farm.sources.base import Cache
    from fa_farm.sources.soil import soilgrids_rootzone
    from fa_farm.sources.ssurgo import ssurgo_rootzone
    from fa_twin.hybrids import crop_table, rooting_override, tsums_for_hybrid
    from fa_twin.soilwater import available_water_at_sowing
    lat, lon, _ = SITES[site]
    cache = Cache(DATA / "cache")
    try:
        _, sp, sprov = ssurgo_rootzone(lat, lon, cache)
    except Exception:  # noqa: BLE001 -- outside the US
        _, sp, sprov = soilgrids_rootzone(lat, lon, cache)
    crop = crop_table("maize", variety)
    wx, winfo = _weather_v4(site)
    mg = management(site, y)
    hist = [r for r in wx if r["DAY"].year <= 2000]
    ph = tsums_for_hybrid(hist, mg["sowing"].strftime("%m-%d"), HYBRID_GDU, crop)
    wav = available_water_at_sowing(wx, sp.SMFCF, sp.SMW, mg["sowing"], sp.RDMSOL, lat=lat,
                                    elev=_hist(site)["elevation_m"], evap_factor=mg["soil_evap_factor"])
    ov = {**ph.overrides(), **rooting_override("maize", sp.RDMSOL, crop["RDMCR"]), "RRI": RRI_MAIZE}
    ov.pop("TSUMEM", None)              # emergence thermal time is calibrated (soil, not air, warms the seed)
    return {"soil": sp, "soil_source": sprov.as_dict(), "overrides": ov, "WAV": wav["WAV_cm"], "wav_info": wav,
            "weather": wx, "weather_info": winfo, "management": mg, "crop": crop}


def _sim_v4(site, y, theta, sci, engine=None, extra=None, remobilisation=False):
    from fa_twin.engine import ZoneConfig, ZoneTwin
    from fa_twin.management import population_overrides
    from fa_twin.weather import records_provider
    d = _hist(site)
    lat, lon, irrigated = SITES[site]
    mg = sci["management"]
    span, rgr, tdwi, tsumem = theta
    base = {"SPAN": span, "RGRLAI": rgr, "TDWI": tdwi, "TSUMEM": tsumem}
    pop = population_overrides("maize", {**sci["crop"], **base}, mg["population"])
    ov = {**sci["overrides"], **base, **pop, **(extra or {})}
    sow, harv = mg["sowing"], dt.date(y, 10, 15)
    wx = [r for r in sci["weather"] if dt.date(y - 1, 11, 1) <= r["DAY"] <= dt.date(y, 12, 31)]
    cfg = ZoneConfig(site, lat, lon, "maize", "Grain_maize_204", sow, harv, soil=sci["soil"], WAV=sci["WAV"],
                     param_overrides=ov, remobilisation=remobilisation)
    tw = ZoneTwin(cfg, records_provider(wx, lat, lon, d["elevation_m"], "POWER + GHCN gauge",
                                        soil_evap_factor=mg["soil_evap_factor"]),
                  engine=engine or ("Wofost72_PP" if irrigated else "Wofost72_WLP_CWB"))
    tw.run_to_end()
    df = tw.output_frame()
    lai = {k.isoformat() if hasattr(k, "isoformat") else str(k): (0.0 if row["LAI"] != row["LAI"] else float(row["LAI"]))
           for k, row in df.iterrows()}
    agb = {k.isoformat() if hasattr(k, "isoformat") else str(k): (0.0 if row["TAGP"] != row["TAGP"] else float(row["TAGP"]) / 1000)
           for k, row in df.iterrows()}
    last = df.iloc[-1]
    return {"lai": lai, "agb_series": agb, "yield_t_ha": float(last["TWSO"]) / 1000 / (1 - MOIST),
            "agb_t_ha": float(last["TAGP"]) / 1000, "dvs_end": float(last["DVS"]), "cfg": cfg, "overrides": ov}


def _loss_v4(theta, cases, gt):
    err = 0.0
    for (site, y), sci in cases:
        r = _sim_v4(site, y, theta, sci)
        t = gt[site][y]
        lai_e = [(r["lai"].get(d.isoformat(), 0.0) - v) / 1.0 for d, v in t["lai"]]
        err += sum(e * e for e in lai_e) / len(lai_e)
        err += ((r["agb_t_ha"] - t["agb_t_ha"]) / 2.0) ** 2 + ((r["yield_t_ha"] - t["yield_t_ha"]) / 1.0) ** 2
    return err


def calibrate_v4() -> dict:
    import numpy as np
    from scipy.optimize import minimize
    gt = ground_truth()
    cases = [((s, y), science_inputs_v4(s, y)) for s, y in TRAIN]
    lo, hi = np.array([b[0] for b in CAL_BOUNDS_V4]), np.array([b[1] for b in CAL_BOUNDS_V4])
    f = lambda z: _loss_v4(lo + (hi - lo) / (1 + np.exp(-z)), cases, gt)          # noqa: E731
    z0 = np.log((np.array(CAL_START_V4) - lo + 1e-6) / (hi - np.array(CAL_START_V4) + 1e-6))
    best = None
    for start in (z0, np.zeros(4)):
        r = minimize(f, start, method="Nelder-Mead", options={"maxiter": 260, "xatol": 1e-3, "fatol": 1e-3})
        if best is None or r.fun < best.fun:
            best = r
    theta = lo + (hi - lo) / (1 + np.exp(-best.x))
    out = {"params": dict(zip(CAL_PARAMS_V4, map(float, theta))), "loss": float(best.fun),
           "start_loss": float(f(z0)), "train": TRAIN, "evals": int(best.nfev), "reference_population": 80000}
    (OUT / "hybrid_calibration_v4.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))
    return out


def run_all_v4(cutoff=(7, 15), seed: int = 0, resume: bool = False) -> dict:
    from fa_farm.anchor import anchor_to_cover
    cal = json.loads((OUT / "hybrid_calibration_v4.json").read_text())["params"]
    theta = [cal[k] for k in CAL_PARAMS_V4]
    p = OUT / "runs_v4.json"
    res = json.loads(p.read_text()) if resume and p.exists() else {}
    for site in SITES:
        lat, lon, irrigated = SITES[site]
        engine = "Wofost72_PP" if irrigated else "Wofost72_WLP_CWB"
        for y in MAIZE[site]:
            if f"{site}_{y}" in res:
                continue
            sci = science_inputs_v4(site, y)
            fwd = _sim_v4(site, y, theta, sci)
            out = {"train": (site, y) in TRAIN, "management": {k: str(v) for k, v in sci["management"].items()},
                   "WAV": sci["WAV"], "soil": sci["soil"].as_dict(),
                   "forward": {k: v for k, v in fwd.items() if k not in ("cfg",)}}
            wx = [r for r in sci["weather"] if dt.date(y - 1, 11, 1) <= r["DAY"] <= dt.date(y, 12, 31)]
            series = _hist(site)["landsat"][y]
            for tag, until in (("anchored", None), ("midseason", dt.date(y, *cutoff))):
                use = [x for x in series if until is None or x[0] <= until]
                ov, info = anchor_to_cover(fwd["cfg"], wx, use, members=24, seed=seed, engine=engine,
                                           soil_evap_factor=sci["management"]["soil_evap_factor"])
                r = _sim_v4(site, y, theta, sci, extra=ov)
                out[tag] = {k: v for k, v in r.items() if k not in ("cfg",)}
                out[tag]["n_obs"] = info.get("n_obs")
            res[f"{site}_{y}"] = out
            print("v4", site, y, "TRAIN" if out["train"] else "test ", " ".join(
                f"{k} {out[k]['yield_t_ha']:.1f}" for k in ("forward", "anchored", "midseason")), flush=True)
            (OUT / "runs_v4.json").write_text(json.dumps(res, default=str))
    return res


if __name__ == "__main__":
    sys.exit(main())
