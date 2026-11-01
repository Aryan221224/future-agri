"""Flight-parameter search for this drone on this field: the drone half of the
"perfect run".

Every candidate (height x speed x pass spacing x carrier rate x RTK) is flown by
the mission simulator under flight-weather samples from the season. A candidate
is scored per treated hectare in money:

    - product used (litres actually sprayed, at the label concentration)
    - operator/aircraft time, valued at the region's drone service price per
      hectare at a reference productivity of 3.5 ha/h (research brief: realistic
      3-4 ha/h for this class), so a slower plan costs more
    - efficacy shortfall: value at risk x (1 - mean dose-efficacy / label efficacy),
      the agronomic price of skipped and under-dosed strips
    - drift: off-target litres valued at (1 + drift_weight) x their product cost
    - RTK amortisation (assumption: 5 % of the service price per ha)

Infeasible conditions (wind, temperature, limits) count against a candidate as
unflyable scenarios; a candidate never flyable is never ranked.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np

from fa_drone.mission import MissionSettings, Weather, simulate_mission
from fa_drone.power import PowerModel
from fa_drone.spray import SprayModel
from fa_twin.damage import dose_efficacy

REF_HA_PER_H = 3.5
RTK_COST_FRAC = 0.05
VOLUME_REF_L_HA = 15.0


@dataclass
class RankedSettings:
    settings: MissionSettings
    score: float                       # money per treated ha (higher is better)
    feasible_frac: float
    kpis_mean: dict
    kpis_p90: dict
    breakdown: dict = field(default_factory=dict)


def volume_factor(rate_l_ha: float) -> float:
    """Efficacy multiplier for carrier volume below the 15 L/ha reference
    (assumption: coverage of foliage falls with volume, exponent 0.25)."""
    return float(min(1.0, (rate_l_ha / VOLUME_REF_L_HA) ** 0.25))


def score_mission(res, settings: MissionSettings, econ, value_at_risk_ha: float, emax: float,
                  targets: np.ndarray | None, drift_weight: float = 1.0) -> tuple[float, dict]:
    area = max(res.kpis["area_ha"], 1e-9)
    product_per_l = econ.fungicide_product_ha / settings.rate_l_ha   # product cost per L of mix
    t = np.ones(len(res.dose_l_ha), bool) if targets is None else np.asarray(targets, bool)
    dose_frac = res.dose_l_ha[t] / settings.rate_l_ha
    eff = float(np.mean(dose_efficacy(dose_frac, emax))) * volume_factor(settings.rate_l_ha)
    product = res.kpis["litres"] * product_per_l / area
    time_cost = (res.kpis["time_min"] / 60.0) * REF_HA_PER_H * econ.drone_service_ha / area
    shortfall = value_at_risk_ha * max(0.0, 1.0 - eff / emax)
    drift = res.kpis["off_target_frac"] * res.kpis["litres"] * product_per_l * (1 + drift_weight) / area
    rtk = RTK_COST_FRAC * econ.drone_service_ha if settings.rtk else 0.0
    total = product + time_cost + shortfall + drift + rtk
    return -total, {"product": round(product, 2), "time": round(time_cost, 2),
                    "efficacy_shortfall": round(shortfall, 2), "drift": round(drift, 2),
                    "rtk": round(rtk, 2), "efficacy": round(eff, 3)}


def optimise_flight(grid, spec, region, econ, winds, value_at_risk_ha: float, emax: float = 0.85,
                    targets: np.ndarray | None = None, heights=(2.0, 2.5, 3.0, 3.5),
                    speeds=(3.0, 4.0, 5.0, 6.0, 7.0), swaths=(3.0, 3.5, 4.0, 4.5, 5.0),
                    rates=(10.0, 15.0, 20.0), rtk=(False, True), pump_lpm: float = 3.5,
                    top: int = 10, seed: int = 0, drift_weight: float = 1.0) -> list[RankedSettings]:
    """Rank flight settings by expected money per treated hectare over ``winds``
    [(wind m/s, from-direction deg, gust m/s, temp C), ...]."""
    pm = PowerModel.calibrate(spec)
    sm = SprayModel(spec)
    out = []
    for h, v, w, r, k in itertools.product(heights, speeds, swaths, rates, rtk):
        if v > min(spec.max_work_speed_ms, region.rules.max_speed_ms):
            continue
        s = MissionSettings(height_m=h, speed_ms=v, swath_m=w, rate_l_ha=r, rtk=k, pump_lpm=pump_lpm)
        scores, kps, brk, flyable = [], [], [], 0
        for i, (ws, wd, wg, tc) in enumerate(winds):
            res = simulate_mission(grid, spec, s, Weather(ws, wd, wg, tc), targets=targets,
                                   seed=seed + i, rules=region.rules, power=pm, spray=sm,
                                   record_track=False)
            if not res.feasible:
                continue
            flyable += 1
            sc, b = score_mission(res, s, econ, value_at_risk_ha, emax, targets, drift_weight)
            scores.append(sc)
            kps.append(res.kpis)
            brk.append(b)
        if not flyable:
            continue
        keys = ("time_min", "litres", "sorties", "battery_swaps", "refills", "cv_pct", "skipped_frac",
                "overdosed_frac", "off_target_frac", "ha_per_hour", "wh_per_ha", "achieved_rate_l_ha")
        mean = {kk: round(float(np.mean([x[kk] for x in kps])), 4) for kk in keys}
        p90 = {kk: round(float(np.percentile([x[kk] for x in kps], 90)), 4) for kk in keys}
        b = {kk: round(float(np.mean([x[kk] for x in brk])), 3) for kk in brk[0]}
        out.append(RankedSettings(s, float(np.mean(scores)), flyable / len(winds), mean, p90, b))
    out.sort(key=lambda x: x.score, reverse=True)
    return out[:top]


def logistics_sensitivity(grid, spec, region, settings: MissionSettings, winds,
                          options=((4, 1), (6, 1), (4, 2), (6, 2)), seed: int = 0) -> list[dict]:
    """Job time for the same flight settings with more battery sets and chargers.
    With one 30-minute charger a 10 L drone flying two tanks per pack becomes
    charger-bound on any field larger than a few hectares."""
    from dataclasses import replace
    pm = PowerModel.calibrate(spec)
    sm = SprayModel(spec)
    rows = []
    for packs, chargers in options:
        s = replace(settings, n_battery_sets=packs, chargers=chargers)
        ks = []
        for i, (ws, wd, wg, tc) in enumerate(winds):
            res = simulate_mission(grid, spec, s, Weather(ws, wd, wg, tc), seed=seed + i,
                                   rules=region.rules, power=pm, spray=sm,
                                   record_track=False)
            if res.feasible:
                ks.append(res.kpis)
        if not ks:
            continue
        rows.append({"battery_sets": packs, "chargers": chargers,
                     "time_min": round(float(np.mean([k["time_min"] for k in ks])), 1),
                     "flight_min": round(float(np.mean([k["flight_min"] for k in ks])), 1),
                     "ha_per_hour": round(float(np.mean([k["ha_per_hour"] for k in ks])), 2),
                     "battery_swaps": round(float(np.mean([k["battery_swaps"] for k in ks])), 1)})
    return rows
