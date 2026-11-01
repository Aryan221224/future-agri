"""Iterations (the perfect run) and plans.

A plan is the operator's choice among the iteration results: the policy the twin
runs, the K3-A Pro flight settings, the scan schedule, and what the season is
expected to take -- operations with their date windows, drone time, crew time,
litres -- from the replicate seasons the iterations flew. Live fields rehearse on
the same field's previous season (real weather, real archived forecasts)."""
from __future__ import annotations

import dataclasses
import datetime as dt

import numpy as np

from fa_farm.archetypes import season_dates
from fa_farm.build import with_region
from fa_farm.sources.forecasts import ForecastSource
from fa_runner.perfect import perfect_run

QUICK_GRID = {"heights": (2.0, 3.0), "speeds": (5.0, 7.0), "swaths": (4.0, 5.0), "rates": (15.0,),
              "rtk": (False, True)}
CREW_SETUP_MIN = 20.0            # assumption: unloading, mixing, checks and packing per mission
CHARGER_VS_SET = 0.6             # assumption: a 12S charger costs ~0.6 of a battery set (2 x 6S 16 Ah)
ASSUMPTIONS = [f"crew time = drone time + {CREW_SETUP_MIN:.0f} min setup per mission (assumption)",
               f"a charger costs ~{CHARGER_VS_SET:g} of a battery set when choosing logistics (assumption)"]


def logistics_pick(rows: list[dict]) -> dict | None:
    """The cheapest battery sets x chargers within 90 % of the best work rate."""
    if not rows:
        return None
    best = max(r["ha_per_hour"] for r in rows)
    ok = [r for r in rows if r["ha_per_hour"] >= 0.9 * best]
    pick = min(ok, key=lambda r: (r["battery_sets"] + CHARGER_VS_SET * r["chargers"], -r["ha_per_hour"]))
    return {**pick, "best_ha_per_hour": best,
            "why": f"cheapest set-up within 90% of the fastest ({best:.2f} ha/h)"}


def _das(day: str, sowing: dt.date) -> int:
    return (dt.date.fromisoformat(day) - sowing).days


def make_plan(farm: dict, result: dict, plan_name: str | None = None) -> dict:
    name = plan_name or result["best"]["plan"]
    row = next((r for r in result["ranked"] if r["plan"] == name), None)
    if row is None:
        raise ValueError(f"the iterations have no plan '{name}'")
    seasons = result.get("seasons_summary", {}).get(name, [])
    r_sow = dt.date.fromisoformat(result["rehearsal"]["sowing"])
    f_sow = dt.date.fromisoformat(farm["sowing"])
    ops = []
    for product in ("herbicide", "fungicide"):
        per = [[m for m in s.get("missions_log", []) if m["product"] == product] for s in seasons]
        flat = [m for ms in per for m in ms]
        if not flat:
            continue
        das = sorted(_das(m["day"], r_sow) for m in flat)
        t = float(np.mean([m["time_min"] for m in flat]))
        ops.append({
            "product": product, "count_mean": round(float(np.mean([len(ms) for ms in per])), 2),
            "das_first": das[0], "das_median": int(np.median(das)), "das_last": das[-1],
            "date_first": (f_sow + dt.timedelta(days=das[0])).isoformat(),
            "date_last": (f_sow + dt.timedelta(days=das[-1])).isoformat(),
            "time_min": round(t, 1), "crew_min": round(t + CREW_SETUP_MIN, 1),
            "sorties": round(float(np.mean([m["sorties"] for m in flat])), 1),
            "battery_swaps": round(float(np.mean([m["battery_swaps"] for m in flat])), 1),
            "refills": round(float(np.mean([m.get("refills", 0) for m in flat])), 1),
            "litres": round(float(np.mean([m["litres"] for m in flat])), 2),
            "area_ha": round(float(np.mean([m["area_ha"] for m in flat])), 3),
            "cv_pct": round(float(np.mean([m["cv_pct"] for m in flat])), 1)})
    scans = float(np.mean([s.get("scans", 0) for s in seasons])) if seasons else 0.0
    if row["policy"].startswith("twin"):
        ops.insert(0, {"product": "scan", "count_mean": round(scans, 2), "das_first": row["scan_das"],
                       "das_median": row["scan_das"], "das_last": row["scan_das"],
                       "date_first": (f_sow + dt.timedelta(days=row["scan_das"])).isoformat(),
                       "date_last": (f_sow + dt.timedelta(days=row["scan_das"])).isoformat(),
                       "gsd_m": row["scan_gsd_m"], "note": "camera-drone scan; extra scouting flights when "
                                                          "the twin is undecided"})
    sprays = [o for o in ops if o["product"] != "scan"]
    totals = {"missions": round(sum(o["count_mean"] for o in sprays), 2),
              "drone_h": round(sum(o["count_mean"] * o["time_min"] for o in sprays) / 60, 2),
              "crew_h": round(sum(o["count_mean"] * o["crew_min"] for o in sprays) / 60, 2),
              "litres": round(sum(o["count_mean"] * o["litres"] for o in sprays), 1),
              "scans": round(scans, 2)}
    return {
        "plan": name, "policy": row["policy"], "flight": row["flight"],
        "scan": {"das": row["scan_das"], "gsd_m": row["scan_gsd_m"],
                 "date": (f_sow + dt.timedelta(days=row["scan_das"])).isoformat()},
        "operations": ops, "totals": totals, "logistics": result.get("logistics", []),
        "logistics_pick": logistics_pick(result.get("logistics", [])),
        "net_mean": row["net_mean"], "cvar20": row["net_cvar20"], "score": row["score"],
        "yield_dry_mean": row["yield_dry_mean"], "currency": result["currency"],
        "value_at_risk_ha": result.get("value_at_risk_ha"), "n_seasons": row["n_seasons"],
        "season": {"sowing": farm["sowing"], "harvest": farm["harvest"]},
        "rehearsal": result["rehearsal"], "warnings": result.get("warnings", []), "assumptions": ASSUMPTIONS}


def perfect_job_factory(state):
    """The ``perfect`` job: rank flight settings and plans for a farm."""

    def job(job: dict, ctx) -> dict:
        fid, p = job["farm_id"], job["params"]
        farm = state.store.get_farm(fid)
        vf = state.farms.load(fid)
        cache = state.settings.data_cache()
        if farm["mode"] == "field":
            sow = dt.date.fromisoformat(farm["sowing"])
            ctx.progress(0.01, f"building the {sow.year - 1} season of this field for the rehearsal")
            rv = state.farms.builder(farm["template"], sow.year - 1, cache, cell_m=vf.spec.cell_m,
                                     seed=vf.spec.seed, polygon_wgs84=vf.spec.polygon_wgs84,
                                     progress=lambda f, m: ctx.progress(0.01 + 0.1 * f, m))
            if farm["region"] and farm["region"] != rv.spec.archetype.region:
                rv.spec = dataclasses.replace(rv.spec, archetype=with_region(rv.spec.archetype, farm["region"]),
                                              region=farm["region"])
            run_vf, base = rv, 0.12
        else:
            run_vf, base = vf, 0.0
        r_sow, r_harv = season_dates(run_vf.spec.archetype, run_vf.spec.year)

        def say(msg: str):
            text = msg.replace("[perfect] ", "")
            if "stage 1" in text:
                ctx.progress(base + 0.3 * (1 - base), text)
            elif "prior yield" in text:
                ctx.progress(base + 0.02, text)
            else:
                ctx.check()

        def season_done(k: int, n: int, name: str, r):
            ctx.progress(base + (1 - base) * (0.3 + 0.7 * k / n), f"season {k}/{n}: {name} (net {r.net_ha:.0f})")

        sources = [ForecastSource.CLIMATOLOGY] if state.settings.offline else None
        pr = perfect_run(run_vf, seeds=tuple(range(int(p.get("seeds", 3)))), plan_members=int(p.get("members", 6)),
                         workers=p.get("workers"), flight_grid=QUICK_GRID if p.get("quick") else None,
                         sources=sources, cache_root=str(cache.root), progress=say, season_done=season_done)
        res = {"summary": {"best": pr.best["plan"], "net_mean": pr.best["net_mean"], "currency": pr.currency},
               "best": pr.best, "ranked": pr.ranked, "flight_ranking": pr.flight_ranking,
               "flight_ranking_patch": pr.flight_ranking_patch, "logistics": pr.logistics,
               "value_at_risk_ha": pr.value_at_risk_ha, "currency": pr.currency, "warnings": pr.warnings,
               "timings_s": pr.timings_s,
               "seasons_summary": {k: [{**r.summary(), "missions_log": r.missions_log} for r in rs]
                                   for k, rs in pr.seasons.items()},
               "rehearsal": {"year": run_vf.spec.year, "sowing": r_sow.isoformat(), "harvest": r_harv.isoformat(),
                             "kind": "previous season of this field" if farm["mode"] == "field" else "this season"},
               "params": p, "area_ha": round(farm["area_ha"] or 0.0, 3)}
        state.store.add_event(fid, "iterations_done", {"job": job["id"], "best": pr.best["plan"],
                                                       "net_mean": pr.best["net_mean"]})
        return res

    return job
