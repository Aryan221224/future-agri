"""Closed-loop season simulation: a ``FieldTwin`` advising a simulated field.

Truth = WOFOST on the true soil and the true weather, coupled day by day to a
late-blight epidemic, with a heterogeneous weed field. The twin sees what a real
deployment would see: the past weather (station), a forecast ensemble, its own
irrigation/spray records, one camera pass, and scouting flights when it asks for
them. Its soil parameters are wrong by up to +-10 % (field capacity, wilting
point), as in the audit.

Policies
--------
``farmer``      fixed 4 cm every 10 days; fungicide every 10 days from day 30 on
                the first sprayable day; blanket herbicide.
``twin``        daily soil-water irrigation trigger; weekly fungicide plan
                (CVaR-80, the default objective) with event-triggered scouting;
                herbicide from the one-pass weed map; the pass assimilated with
                the EnKF.
``twin_mean``   as ``twin`` with the risk-neutral (mean) objective.
``twin_alert``  as ``twin`` but with a regional inoculum alert (truth of whether
                the pathogen arrived) -- the input the audit identified as missing.
``none``        no irrigation beyond the farmer schedule, no sprays (baseline for
                the value of protection).

Economics are ``PotatoEconomics`` (stated assumptions). Results are simulated,
not field-measured, and the blight model makes ~65 % of seasons infected.
"""
from __future__ import annotations

import copy
import datetime as dt
import logging
from dataclasses import dataclass, field

import numpy as np

from fa_planner.sop import DEFAULT_WINDOW
from fa_twin.damage import Blight, infection_day, weed_loss_frac
from fa_twin.engine import SoilParams, ZoneConfig, ZoneTwin
from fa_twin.weather import forecast_ensemble, forecast_member, generate_records, records_provider

from .orchestrator import FieldTwin, TwinSettings

log = logging.getLogger(__name__)

LAT, LON = 28.45, 77.58
PLANT, HARVEST = dt.date(2025, 10, 25), dt.date(2026, 2, 10)
WSTART = dt.date(2025, 10, 1)
PASS_DAY = 35
POLICIES = ("none", "farmer", "twin", "twin_mean", "twin_alert")


def potato_cfg(soil: SoilParams | None = None) -> ZoneConfig:
    return ZoneConfig(zone_id="bennett-potato", latitude=LAT, longitude=LON, crop="potato",
                      variety="Potato_701", sowing_date=PLANT, harvest_date=HARVEST,
                      soil=soil or SoilParams())


@dataclass
class SeasonResult:
    policy: str
    seed: int
    infected: bool
    fresh_t_ha_farm: float
    water_cm: float
    irrigations: int
    sprays: int
    flights: int
    herbicide: str
    weed_loss_pct: float
    final_severity: float
    net_rs_ha: float
    log: list = field(default_factory=list)


def _short_forecast(truth: list[dict], day: dt.date, seed: int, days: int = 3) -> list[list[dict]]:
    """One forecast member for the daily irrigation check, with the same lead-time
    error model as the weekly ensemble (rain mis-scaled, sometimes missed or
    invented). The audit's version never missed or invented rain, which flattered
    the water saving."""
    return [forecast_member(truth, day, seed, forecast_days=days)]


def simulate_season(seed: int, policy: str, settings: TwinSettings | None = None,
                    plan_members: int = 10, keep_log: bool = False) -> SeasonResult:
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    rng = np.random.default_rng(seed)
    truth = generate_records(WSTART, 200, 20_000 + seed)
    by_day = {r["DAY"]: r for r in truth}
    s = copy.deepcopy(settings) if settings else TwinSettings()
    s.plan_members = plan_members
    if policy == "twin_mean":
        s.objective = "mean"
    econ = s.econ

    # truth field
    inf = infection_day(truth, PLANT, np.random.default_rng(30_000 + seed), s.blight)
    inoculum_arrived = inf is not None or bool(np.random.default_rng(30_000 + seed).random()
                                               < s.blight.p_inoculum)
    blight = Blight(inf, s.blight)
    true_model = ZoneTwin(potato_cfg(), records_provider(truth, LAT, LON), engine="Wofost72_WLP_CWB")
    cells = np.exp(rng.normal(np.log(12.0), 0.9, 100))                 # weeds/m2 per cell
    est_cells = cells * np.exp(rng.normal(0, 0.3, 100))               # one-pass estimate

    # the twin (soil wrong by up to +-10 %)
    twin = None
    if policy.startswith("twin"):
        err = SoilParams(SMFCF=0.32 * rng.uniform(0.9, 1.1), SMW=0.15 * rng.uniform(0.9, 1.1))
        # The twin receives weather the way a deployment does: what has been
        # observed up to the day before planting, then one day at a time.
        twin = FieldTwin(potato_cfg(err), [r for r in truth if r["DAY"] < PLANT], s,
                         inoculum=(inoculum_arrived if policy == "twin_alert" else None))

    from pcse import signals
    water = 0.0
    irrigations = 0
    sprays: list[dt.date] = []
    flights = 0
    pending: dt.date | None = None
    pending_until: dt.date | None = None
    herbicide = "blanket" if policy in ("farmer",) else "none"
    treated = np.ones(100, bool) if herbicide == "blanket" else np.zeros(100, bool)
    herb_cost = econ.herbicide_blanket if herbicide == "blanket" else 0.0
    farmer_next_spray = PLANT + dt.timedelta(days=30)

    # Decisions on day d are made from the state at the end of day d-1, by truth
    # and twin alike (the twin has observed weather through d-1), so an
    # irrigation signal enters the same day's water balance in both.
    d = PLANT
    while d <= HARVEST and not true_model.terminated:
        rec = by_day[d]
        dap = (d - PLANT).days

        # ---- irrigation
        irrigate = False
        if policy in ("farmer", "none"):
            irrigate = dap > 0 and dap % 10 == 0
        elif twin is not None:
            adv = twin.irrigation_advice(d, _short_forecast(truth, d, seed * 100_000 + dap))
            irrigate = adv.action
        if irrigate:
            true_model.model._send_signal(signal=signals.irrigate, amount=4.0, efficiency=0.7)
            if twin is not None:
                twin.record_irrigation(d, 4.0)
            water += 4.0
            irrigations += 1

        # ---- one camera pass (twin policies)
        if twin is not None and dap == PASS_DAY:
            st = true_model.state()
            lai = float(st.get("LAI") or 0.0)
            cc = float(np.clip(1 - np.exp(-0.6 * lai) + rng.normal(0, 0.04), 0.0, 0.985))
            dvs = float((st.get("DVS") or 0.0) + rng.normal(0, 0.05))
            twin.ingest_pass(d, cc, dvs, est_cells)
            herbicide = twin.herbicide.option
            if herbicide == "blanket":
                treated, herb_cost = np.ones(100, bool), econ.herbicide_blanket
            elif herbicide == "spot":
                thr = twin.herbicide.threshold_m2
                treated = est_cells > thr
                herb_cost = econ.herbicide_spot_fixed + econ.herbicide_spot_per_fraction * treated.mean()

        # ---- fungicide
        spray_today = False
        if policy == "farmer" and d >= farmer_next_spray and DEFAULT_WINDOW.ok(rec):
            spray_today = True
            farmer_next_spray = d + dt.timedelta(days=10)
        if twin is not None:
            if dap >= 28 and (dap - 28) % 7 == 0:
                fc = forecast_ensemble(truth, d, plan_members, seed=seed * 1000 + dap)
                plan = twin.weekly_plan(d, fc, seed=seed * 7919 + dap)
                if plan.get("scout"):
                    flights += 1
                    sev = blight.S * float(np.exp(rng.normal(0, 0.3))) if blight.S >= 0.005 else None
                    twin.record_scouting(d, sev)      # a negative flight is information too
                    plan = twin.weekly_plan(d, fc, seed=seed * 7919 + dap + 1)
                fd = plan["fungicide"]
                pending = fd.spray_day
                pending_until = d + dt.timedelta(days=6)
            if pending is not None and d >= pending:
                if DEFAULT_WINDOW.ok(rec):
                    spray_today, pending = True, None
                elif d >= pending_until:
                    pending = None
        if spray_today:
            blight.spray(d)
            sprays.append(d)
            if twin is not None:
                twin.record_spray(d)

        # ---- truth dynamics
        dS = blight.step(rec)
        true_model.model.run(days=1)
        if twin is not None:
            twin.add_weather([rec])            # day d is observed once it is over
        lai = true_model.model.get_variable("LAI")
        if dS > 0 and lai:
            healthy = max(1e-3, 1.0 - blight.S + dS)
            true_model.model.set_variable("LAI", max(0.01, lai * (1.0 - dS / healthy)))
        d += dt.timedelta(days=1)

    true_model.model.run_till_terminate()
    y = float((true_model.model.get_summary_output() or [{}])[0].get("TWSO") or 0.0)
    dens = np.where(treated, cells * (1 - s.weeds.herbicide_efficacy), cells)
    wl = float(np.mean([weed_loss_frac(x, s.weeds) for x in dens]))
    y_after = y * (1 - wl)
    net = (econ.revenue(y_after) - econ.fungicide_per_spray * len(sprays)
           - econ.scouting_per_flight * flights - herb_cost - econ.irrigation_per_cm * water)
    fresh_farm = y_after / econ.dry_matter / 1000 * econ.farm_factor
    return SeasonResult(policy, seed, inf is not None, round(fresh_farm, 2), water, irrigations,
                        len(sprays), flights, herbicide, round(100 * wl, 1), round(blight.S, 3),
                        round(net), twin.advice_log() if (twin is not None and keep_log) else [])
