"""The "perfect run": the best plan this drone can actually fly on this farm.

Stage 1 -- drone. Flight settings (height, speed, pass spacing, carrier rate,
RTK) are searched with the mission simulator under flight-weather samples drawn
from the site's own history in the season's months (days that pass the spray
window), on a representative patch of the field, then the leaders are re-flown
over the whole field. Score: money per treated hectare (product, time, efficacy
shortfall from skips and under-dosing, drift, RTK).

Stage 2 -- season. Candidate plans (no sprays, calendar, twin with the
operator's default settings, twin with the stage-1 settings, twin with the
regional inoculum alert, cheaper scans) are each run as a full closed-loop
season over replicate ground truths (blight arrival and weed field drawn per
seed; weather is the real season). Ranking: E[net] - lambda x (E[net] - CVaR20),
i.e. expected return with a penalty for the bad tail.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field, replace

import numpy as np
from shapely.geometry import box

from fa_drone.mission import MissionSettings
from fa_drone.spec import load_spec
from fa_farm.archetypes import season_dates
from fa_farm.grid import FarmGrid
from fa_farm.regions import load_region
from fa_twin.engine import ZoneConfig, ZoneTwin
from fa_twin.weather import records_provider

from .economics import Economics
from .optimise import logistics_sensitivity, optimise_flight
from .scenarios import ForecastFeed, spray_window, wind_samples
from .season import run_season


@dataclass
class PlanSpec:
    name: str
    policy: str
    flight: MissionSettings
    scan_gsd_m: float = 0.005
    scan_das: int = 45


@dataclass
class PerfectRun:
    best: dict
    ranked: list
    flight_ranking: list
    seasons: dict
    value_at_risk_ha: float
    currency: str
    flight_ranking_patch: list = field(default_factory=list)
    logistics: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    timings_s: dict = field(default_factory=dict)


def _patch(vf, max_ha: float = 2.0):
    """A representative sub-field (square at the centroid) for the flight search."""
    g = FarmGrid.from_wgs84(vf.spec.polygon_wgs84, 5.0)
    if g.area_ha <= max_ha:
        return g
    from fa_core.geo import to_wgs84
    c = g.polygon_local.centroid
    half = (max_ha * 1e4) ** 0.5 / 2
    sq = box(c.x - half, c.y - half, c.x + half, c.y + half).intersection(g.polygon_local)
    if sq.area < 0.5 * max_ha * 1e4:
        return g
    return FarmGrid.from_wgs84(to_wgs84(sq, g.crs), 5.0)


def prior_yield(vf) -> float:
    """Twin-side prior: shipped parameters, SoilGrids soil, the previous year's
    observed weather (no truth used)."""
    a = vf.spec.archetype
    sow, harv = season_dates(a, vf.spec.year - 1)
    recs = [r for r in vf.history if sow - dt.timedelta(days=5) <= r["DAY"] <= harv + dt.timedelta(days=2)]
    if not recs or recs[-1]["DAY"] < harv:
        return 0.0
    lat, lon = vf.spec.centroid
    sci = vf.spec.science_inputs() if hasattr(vf.spec, "science_inputs") else {}
    cfg = ZoneConfig("prior", lat, lon, a.crop, a.variety, sow, harv, soil=vf.spec.soil,
                     WAV=sci.get("WAV", 25.0), soil_evap_factor=sci.get("soil_evap_factor", 1.0),
                     param_overrides=dict(sci.get("overrides") or {}))
    zt = ZoneTwin(cfg, records_provider(recs, lat, lon, vf.spec.elevation_m,
                                        soil_evap_factor=cfg.soil_evap_factor),
                  engine="Wofost72_PP" if a.irrigated else "Wofost72_WLP_CWB")
    zt.run_to_end()
    return float(zt.summary().get("TWSO") or 0.0)


def _job(args):
    vf, plan, seed, plan_members, sources, cache_root = args
    from fa_farm.sources.base import Cache
    lat, lon = vf.spec.centroid
    feed = ForecastFeed(lat, lon, vf.history, Cache(cache_root) if cache_root else None, sources)
    r = run_season(vf, plan.policy, seed=seed, feed=feed, flight=plan.flight, scan_das=plan.scan_das,
                   scan_gsd_m=plan.scan_gsd_m, plan_members=plan_members)
    return plan.name, r


def cvar_low(v, q: float = 0.2) -> float:
    v = np.sort(np.asarray(v, float))
    k = max(1, int(np.ceil(q * len(v))))
    return float(v[:k].mean())


def perfect_run(vf, seeds=(0, 1, 2), lam: float = 0.5, workers: int | None = None,
                plan_members: int = 10, n_winds: int = 6, flight_grid: dict | None = None,
                sources=None, cache_root: str | None = None, progress=print,
                season_done=None) -> PerfectRun:
    t0 = time.time()
    spec = load_spec()
    a = vf.spec.archetype
    region = load_region(a.region)
    econ = Economics.from_region(region, a.crop)
    sow, harv = season_dates(a, vf.spec.year)
    months = {m for m in range(1, 13)
              if any(d.month == m for d in (sow + dt.timedelta(days=k) for k in range((harv - sow).days)))}
    winds = wind_samples(vf.history, months, spray_window(region, spec), n_winds, seed=0)
    warnings = list(vf.warnings) + region.warnings()
    if not winds:
        warnings.append("no historical day in the season passes the spray window; flight search used calm air")
        winds = [(1.0, 270.0, 1.6, 20.0)]

    y0 = prior_yield(vf)
    var_ha = 0.10 * econ.revenue(y0) if y0 > 0 else 0.10 * econ.revenue(5000.0)
    progress(f"[perfect] prior yield {y0:.0f} kg DM/ha -> value at risk {var_ha:.0f} {econ.currency}/ha per application")

    fg = dict(heights=(2.0, 2.5, 3.0, 3.5), speeds=(3.0, 4.0, 5.0, 6.0, 7.0), swaths=(3.0, 4.0, 5.0),
              rates=(10.0, 15.0), rtk=(False, True))
    fg.update(flight_grid or {})
    patch = _patch(vf)
    ranked = optimise_flight(patch, spec, region, econ, winds[:4], var_ha, top=8, **fg)
    t1 = time.time()
    progress(f"[perfect] stage 1: {len(ranked)} settings ranked on a {patch.area_ha:.1f} ha patch in {t1 - t0:.0f} s")
    full = FarmGrid.from_wgs84(vf.spec.polygon_wgs84, vf.spec.cell_m)
    final = []
    for r in ranked[:3]:
        rr = optimise_flight(full, spec, region, econ, winds, var_ha,
                             heights=(r.settings.height_m,), speeds=(r.settings.speed_ms,),
                             swaths=(r.settings.swath_m,), rates=(r.settings.rate_l_ha,),
                             rtk=(r.settings.rtk,), top=1)
        final += rr
    final.sort(key=lambda x: x.score, reverse=True)
    best_flight = final[0].settings if final else MissionSettings()
    logistics = logistics_sensitivity(full, spec, region, best_flight, winds[:2])
    progress(f"[perfect] best flight: h={best_flight.height_m} m v={best_flight.speed_ms} m/s "
             f"spacing={best_flight.swath_m} m rate={best_flight.rate_l_ha} L/ha rtk={best_flight.rtk}")

    default = MissionSettings()
    plans = [PlanSpec("none", "none", default), PlanSpec("calendar", "calendar", default),
             PlanSpec("twin_default_flight", "twin", default),
             PlanSpec("twin_optimised_flight", "twin", best_flight),
             PlanSpec("twin_optimised_cheap_scan", "twin", best_flight, scan_gsd_m=0.01),
             PlanSpec("twin_optimised_early_scan", "twin", best_flight, scan_das=30)]
    if "late_blight" in a.diseases:
        plans.append(PlanSpec("twin_alert_optimised_flight", "twin_alert", best_flight))
    jobs = [(vf, p, s, plan_members, sources, cache_root) for p in plans for s in seeds]
    workers = workers or max(1, (os.cpu_count() or 2))
    seasons: dict = {p.name: [] for p in plans}
    def done(name, r):
        seasons[name].append(r)
        if season_done is not None:
            season_done(sum(len(v) for v in seasons.values()), len(jobs), name, r)

    if workers > 1:
        import multiprocessing as mp
        # spawn: safe when called from a threaded server (fork can inherit held locks)
        with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn")) as ex:
            for name, r in ex.map(_job, jobs):
                done(name, r)
    else:
        for j in jobs:
            done(*_job(j))
    t2 = time.time()
    progress(f"[perfect] stage 2: {len(jobs)} closed-loop seasons in {t2 - t1:.0f} s")

    table = []
    for p in plans:
        rs = seasons[p.name]
        nets = [r.net_ha for r in rs]
        mean, cv = float(np.mean(nets)), cvar_low(nets)
        table.append({
            "plan": p.name, "policy": p.policy, "flight": asdict(p.flight), "scan_gsd_m": p.scan_gsd_m,
            "scan_das": p.scan_das,
            "net_mean": round(mean, 2), "net_cvar20": round(cv, 2),
            "score": round(mean - lam * (mean - cv), 2),
            "yield_dry_mean": round(float(np.mean([r.yield_dry_kg_ha for r in rs])), 1),
            "fungicide_sprays_mean": round(float(np.mean([r.fungicide_sprays for r in rs])), 2),
            "litres_mean": round(float(np.mean([r.litres for r in rs])), 2),
            "sorties_mean": round(float(np.mean([r.sorties for r in rs])), 1),
            "drone_time_min_mean": round(float(np.mean([r.drone_time_min for r in rs])), 1),
            "off_target_l_mean": round(float(np.mean([r.off_target_l for r in rs])), 3),
            "cv_pct_mean": round(float(np.mean([r.mean_cv_pct for r in rs])), 1),
            "skipped_frac_mean": round(float(np.mean([r.mean_skipped_frac for r in rs])), 4),
            "scans_mean": round(float(np.mean([r.scans for r in rs])), 1),
            "n_seasons": len(rs)})
    table.sort(key=lambda x: x["score"], reverse=True)
    best = dict(table[0])
    best["jiyi_settings"] = asdict(best_flight) if "optimised" in best["plan"] else asdict(default)
    def rows(rs, scale):
        return [dict(scale=scale, settings=asdict(r.settings), score=round(r.score, 2),
                     feasible_frac=r.feasible_frac, kpis_mean=r.kpis_mean, kpis_p90=r.kpis_p90,
                     breakdown=r.breakdown) for r in rs]
    best["logistics"] = logistics
    return PerfectRun(best=best, ranked=table,
                      flight_ranking=rows(final, f"whole field {full.area_ha:.1f} ha"),
                      seasons=seasons, value_at_risk_ha=round(var_ha, 2), currency=econ.currency,
                      flight_ranking_patch=rows(ranked, f"patch {patch.area_ha:.1f} ha"),
                      logistics=logistics, warnings=warnings,
                      timings_s={"stage1": round(t1 - t0, 1), "stage2": round(t2 - t1, 1)})
