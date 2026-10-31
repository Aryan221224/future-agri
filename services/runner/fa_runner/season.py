"""One closed-loop season on a virtual farm.

Each morning: the truth has run through yesterday; the twin receives yesterday's
observed weather. On the scan day the separate camera drone scans the truth and
the twin ingests the DigitalFarm (EnKF on cover and stage; herbicide from the
weed map). Weekly from 4 weeks after sowing the twin plans fungicide with the
real ensemble forecast issued that day (``ForecastFeed``), scouting with a
coarser camera flight when the plan is undecided. Every spray is flown by the
drone twin on the first day whose *observed* weather passes the spray window;
what it actually deposited is applied to the truth. At harvest the truth's
yield and every cost give the season's net return.

Policies
--------
``none``        no sprays (value-of-protection baseline)
``calendar``    blanket herbicide 30 days after sowing; fungicide every 10 days
                from day 30 (potato) -- the conventional schedule
``twin``        scan-driven herbicide (none/spot/blanket) and forecast-planned
                fungicide with event-triggered scouting
``twin_alert``  ``twin`` plus a regional inoculum alert (whether the pathogen arrived)
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

import numpy as np

from fa_drone.mission import MissionSettings, simulate_mission
from fa_drone.power import PowerModel
from fa_drone.spec import DroneSpec, load_spec
from fa_drone.spray import SprayModel
from fa_farm.regions import load_region
from fa_farm.truth import AsApplied, GroundTruthFarm
from fa_scan.digital import build_digital_farm
from fa_scan.measure import simulate_scan
from fa_scan.survey import plan_scan

from .economics import Economics
from .optimise import REF_HA_PER_H
from .scenarios import ForecastFeed, flight_record, mission_weather, spray_window
from .twinfarm import TwinFarm

POLICIES = ("none", "calendar", "twin", "twin_alert")
#: Days a due job may wait for flyable weather. Herbicide stays useful for two weeks
#: (weeds grow but are still controlled); a fungicide not flown within a week is
#: re-decided by the next weekly plan instead.
MAX_WAIT_DAYS = {"herbicide": 14, "fungicide": 6}


@dataclass
class SeasonResult:
    policy: str
    seed: int
    yield_dry_kg_ha: float
    yield_marketed_kg_ha: float
    revenue_ha: float
    costs_ha: dict
    net_ha: float
    currency: str
    fungicide_sprays: int
    herbicide: str
    herbicide_treated_frac: float
    missions: int
    litres: float
    sorties: int
    battery_swaps: int
    drone_time_min: float
    off_target_l: float
    mean_cv_pct: float
    mean_skipped_frac: float
    scans: int
    blight_infection_day: str | None
    final_blight_severity: float
    weed_loss_pct: float
    forecast_sources: dict
    flight: dict
    decisions: list = field(default_factory=list)
    missions_log: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    as_applied_last: list | None = None

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("decisions")
        d.pop("missions_log")
        d.pop("as_applied_last")
        return d


def run_season(vf, policy: str, seed: int = 0, feed: ForecastFeed | None = None,
               spec: DroneSpec | None = None, flight: MissionSettings | None = None,
               scan_das: int = 45, scan_gsd_m: float = 0.005, camera: str = "p4rtk",
               scout_gsd_m: float = 0.01, plan_members: int = 10, objective: str = "mean",
               cache=None, expected_yield_kg_ha: float | None = None) -> SeasonResult:
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}")
    spec = spec or load_spec()
    flight = flight or MissionSettings()
    a = vf.spec.archetype
    region = load_region(a.region)
    econ = Economics.from_region(region, a.crop)
    window = spray_window(region, spec)
    rng = np.random.default_rng(10_000 + seed)
    spec_seeded = vf.spec.__class__(**{**vf.spec.__dict__, "seed": vf.spec.seed * 1000 + seed})
    truth = GroundTruthFarm(spec_seeded, vf.season_weather)
    grid = truth.grid
    lat, lon = vf.spec.centroid
    by_day = {r["DAY"]: r for r in vf.season_weather}
    feed = feed or ForecastFeed(lat, lon, vf.history, cache)
    pm, sm = PowerModel.calibrate(spec), SprayModel(spec)
    notes = list(region.warnings())

    twin = None
    if policy.startswith("twin"):
        inoc = (truth.blight_infection_day is not None) if policy == "twin_alert" else None
        first = vf.season_weather[0]["DAY"]
        obs0 = [r for r in vf.season_weather if first <= r["DAY"] < truth.sowing]
        if expected_yield_kg_ha is None:
            from .perfect import prior_yield
            expected_yield_kg_ha = prior_yield(vf) or None
        twin = TwinFarm(a, region, lat, lon, obs0, vf.spec.soil, truth.sowing, truth.harvest_date,
                        inoculum=inoc, objective=objective, plan_members=plan_members,
                        expected_yield_kg_ha=expected_yield_kg_ha)

    costs = {"fungicide_product": 0.0, "herbicide_product": 0.0, "application": 0.0, "scans": 0.0}
    pending: list[dict] = []       # {"product", "from": date, "targets": mask|None}
    missions_log, cvs, skips = [], [], []
    st = {"litres": 0.0, "sorties": 0, "swaps": 0, "time": 0.0, "off": 0.0, "fung": 0, "scans": 0,
          "herb": "none", "herb_frac": 0.0}
    last_aa = None
    blight_crop = truth.blight_modelled
    windy_days = 0

    def fly(d: dt.date, job: dict) -> bool:
        nonlocal last_aa, windy_days
        rec = by_day[d]
        if not window.ok(flight_record(rec)):
            windy_days += 1
            return False
        res = simulate_mission(grid, spec, flight, mission_weather(rec, rng), targets=job["targets"],
                               seed=seed * 97 + len(missions_log), rules=region.rules, power=pm, spray=sm)
        if not res.feasible:
            windy_days += 1
            return False
        frac = res.dose_l_ha / flight.rate_l_ha
        truth.apply(AsApplied(d, job["product"], frac, res.kpis["off_target_frac"] * res.kpis["litres"],
                              res.kpis["litres"]))
        last_aa = frac.round(3).tolist()
        prod_ha = econ.fungicide_product_ha if job["product"] == "fungicide" else econ.herbicide_product_ha
        field_ha = grid.area_ha
        # product at label concentration for the litres actually sprayed
        costs[f"{job['product']}_product"] += prod_ha * res.kpis["litres"] / (flight.rate_l_ha * field_ha)
        costs["application"] += (res.kpis["time_min"] / 60) * REF_HA_PER_H * econ.drone_service_ha / field_ha
        st["litres"] += res.kpis["litres"]
        st["sorties"] += res.kpis["sorties"]
        st["swaps"] += res.kpis["battery_swaps"]
        st["time"] += res.kpis["time_min"]
        st["off"] += res.kpis["off_target_frac"] * res.kpis["litres"]
        cvs.append(res.kpis["cv_pct"])
        skips.append(res.kpis["skipped_frac"])
        missions_log.append({"day": d.isoformat(), "product": job["product"], **res.kpis,
                             "warnings": res.warnings})
        if job["product"] == "fungicide":
            st["fung"] += 1
            if twin is not None:
                twin.record_spray(d)
        return True

    def scan(d: dt.date, gsd: float, purpose: str):
        state = truth.state()
        sp = plan_scan(grid, camera, gsd)
        obs = simulate_scan(state, grid, sp, rng, crop=a.crop,
                            days_after_sowing=(d - truth.sowing).days)
        costs["scans"] += econ.scan_ha
        st["scans"] += 1
        return obs, sp

    d = truth.sowing + dt.timedelta(days=1)
    while d <= truth.harvest_date:
        truth.advance_to(d - dt.timedelta(days=1))
        if twin is not None:
            twin.add_weather([by_day[d - dt.timedelta(days=1)]])
        das = (d - truth.sowing).days

        # ---- herbicide
        if a.weeds:
            if policy == "calendar" and das == 30:
                pending.append({"product": "herbicide", "from": d, "targets": None})
                st["herb"], st["herb_frac"] = "blanket", 1.0
            if twin is not None and das == scan_das:
                obs, _ = scan(d, scan_gsd_m, "pass")
                dfarm = build_digital_farm(obs, grid)
                twin.ingest(dfarm, d - dt.timedelta(days=1), seed=seed)
                st["herb"] = twin.herbicide_option
                tg = twin.weed_targets()
                st["herb_frac"] = float(tg.mean())
                if tg.any():
                    pending.append({"product": "herbicide", "from": d,
                                    "targets": None if tg.all() else tg})
        elif twin is not None and das == scan_das:
            obs, _ = scan(d, scan_gsd_m, "pass")
            twin.ingest(build_digital_farm(obs, grid), d - dt.timedelta(days=1), seed=seed)

        # ---- fungicide
        if blight_crop and das >= 28:
            if policy == "calendar" and (das - 30) % 10 == 0 and das >= 30:
                pending.append({"product": "fungicide", "from": d, "targets": None})
            if twin is not None and (das - 28) % 7 == 0:
                observed = twin.field.records
                mem = feed.members(d, observed, truth.harvest_date, plan_members, seed=seed * 31 + das)
                plan = twin.weekly_plan(d, mem, seed=seed * 7919 + das)
                if plan.get("scout"):
                    obs, _ = scan(d, scout_gsd_m, "scout")
                    twin.record_scouting(d, obs.blight_sev_est if obs.blight_flag.any() else None)
                    plan = twin.weekly_plan(d, mem, seed=seed * 7919 + das + 1)
                fd = plan.get("fungicide")
                if fd is not None and fd.spray_day is not None and fd.spray_day <= d + dt.timedelta(days=6):
                    if not any(j["product"] == "fungicide" for j in pending):
                        pending.append({"product": "fungicide", "from": max(d, fd.spray_day),
                                        "targets": None})

        # ---- fly what is due and flyable today
        keep = []
        for job in pending:
            if job["from"] > d:
                keep.append(job)
                continue
            if fly(d, job):
                continue
            wait = MAX_WAIT_DAYS[job["product"]]
            if (d - job["from"]).days < wait:
                keep.append(job)
            else:
                notes.append(f"{job['product']} due {job['from']} dropped: no feasible spray window "
                             f"within {wait} days")
        pending = keep
        d += dt.timedelta(days=1)

    for job in pending:
        notes.append(f"{job['product']} due {job['from']} not flown before harvest: no feasible "
                     "spray window")
    h = truth.harvest()
    rev = econ.revenue(h.field_yield_kg_ha)
    cost_total = sum(costs.values())
    if twin is not None:
        decisions = twin.field.advice_log()
    else:
        decisions = []
    if windy_days and not missions_log and pending == [] and not any("no feasible" in n for n in notes):
        notes.append("no feasible spray window on any due day")
    return SeasonResult(
        policy=policy, seed=seed, yield_dry_kg_ha=round(h.field_yield_kg_ha, 1),
        yield_marketed_kg_ha=round(h.field_yield_kg_ha / econ.dry_matter, 1), revenue_ha=round(rev, 2),
        costs_ha={k: round(v, 2) for k, v in costs.items()}, net_ha=round(rev - cost_total, 2),
        currency=econ.currency, fungicide_sprays=st["fung"], herbicide=st["herb"],
        herbicide_treated_frac=round(st["herb_frac"], 3), missions=len(missions_log),
        litres=round(st["litres"], 2), sorties=st["sorties"], battery_swaps=st["swaps"],
        drone_time_min=round(st["time"], 1), off_target_l=round(st["off"], 2),
        mean_cv_pct=round(float(np.mean(cvs)), 1) if cvs else 0.0,
        mean_skipped_frac=round(float(np.mean(skips)), 4) if skips else 0.0, scans=st["scans"],
        blight_infection_day=h.blight_infection_day.isoformat() if h.blight_infection_day else None,
        final_blight_severity=round(h.blight_final_mean, 4), weed_loss_pct=round(100 * h.weed_loss_mean, 2),
        forecast_sources=dict(feed.sources_used), flight=asdict(flight), decisions=decisions,
        missions_log=missions_log, notes=notes, as_applied_last=last_aa)
