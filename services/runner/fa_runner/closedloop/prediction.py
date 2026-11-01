"""What the plan predicted before the run, and what counts as a deviation from it.

Only |z| > 2 over enough cells is a deviation (at least 5 cells and 5 % of the cells
observed), so noise is never chased. A cell the twin has no prediction for (infinite
spread) or the camera did not see is not scored.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

Z_LIMIT = 2.0
MIN_CELLS = 5
MIN_SHARE = 0.05
DONE_SHARE = 0.9          # a cell is done at >= 90 % of its prescribed dose (spec)
CC_SD = 0.08              # assumed: the twin's canopy-cover spread (field-level twin)
K_COVER = 0.6             # canopy cover = 1 - exp(-K * LAI), as in fa_farm.truth


@dataclass
class Prediction:
    op_id: str
    product: str
    weeds_mean: np.ndarray
    weeds_sd: np.ndarray
    cc_mean: float
    cc_sd: float
    targets: np.ndarray
    dose_l_ha: np.ndarray
    sorties: int
    time_min: float
    litres: float
    made_at: str

    def to_json(self) -> dict:
        def arr(a, d=3):
            return [None if not np.isfinite(v) else round(float(v), d) for v in np.asarray(a, float)]
        return {"op_id": self.op_id, "product": self.product, "weeds_mean": arr(self.weeds_mean),
                "weeds_sd": arr(self.weeds_sd), "cc_mean": round(self.cc_mean, 3), "cc_sd": self.cc_sd,
                "targets": np.where(self.targets)[0].tolist(), "dose_l_ha": arr(self.dose_l_ha, 2),
                "sorties": int(self.sorties), "time_min": round(self.time_min, 1),
                "litres": round(self.litres, 2), "made_at": self.made_at}


@dataclass
class Deviation:
    channel: str                 # "weeds" | "canopy" | "dose" | "track"
    cells: np.ndarray
    z_or_ratio: np.ndarray
    significant: bool
    summary: str
    correction: str

    def to_json(self) -> dict:
        return {"channel": self.channel, "cells": self.cells.tolist(),
                "values": [round(float(v), 2) for v in self.z_or_ratio], "significant": self.significant,
                "summary": self.summary, "correction": self.correction}


def enough(n_flagged: int, n_scored: int) -> bool:
    return n_flagged >= MIN_CELLS and n_flagged >= MIN_SHARE * max(n_scored, 1)


def deviation_weeds(pred: Prediction, obs_est: np.ndarray, obs_sd: np.ndarray, observed: np.ndarray) -> Deviation:
    obs_est, obs_sd = np.asarray(obs_est, float), np.asarray(obs_sd, float)
    sd = np.sqrt(pred.weeds_sd ** 2 + obs_sd ** 2)
    ok = np.asarray(observed, bool) & np.isfinite(obs_est) & np.isfinite(sd) & (sd > 0)
    idx = np.where(ok)[0]
    z = (obs_est[idx] - pred.weeds_mean[idx]) / sd[idx]
    hit = np.abs(z) > Z_LIMIT
    sig = enough(int(hit.sum()), len(idx))
    more = int((z[hit] > 0).sum())
    summary = (f"{int(hit.sum())} of {len(idx)} scored cells differ from the prediction by more than "
               f"{Z_LIMIT:g} sd ({more} more weeds, {int(hit.sum()) - more} fewer)" if len(idx)
               else "no cell with both a prediction and an observation")
    return Deviation("weeds", idx[hit], z[hit], sig, summary, "re-plan remaining targets" if sig else "")


def deviation_dose(pred: Prediction, dose_cum: np.ndarray, flown: np.ndarray) -> Deviation:
    planned = np.asarray(pred.dose_l_ha, float)
    sel = np.asarray(flown, bool) & np.asarray(pred.targets, bool) & (planned > 0)
    idx = np.where(sel)[0]
    ratio = np.asarray(dose_cum, float)[idx] / planned[idx]
    low = ratio < DONE_SHARE
    sig = enough(int(low.sum()), len(idx))
    summary = (f"{int(low.sum())} of {len(idx)} flown target cells got under {DONE_SHARE:.0%} of the "
               "planned dose" if len(idx) else "no flown target cell yet")
    return Deviation("dose", idx[low], ratio[low], sig, summary, "re-fly under-dosed cells" if sig else "")


def _twin_cc(session) -> float:
    lai = None
    if session.trace and session.trace[-1].get("twin_lai") is not None:
        lai = session.trace[-1]["twin_lai"]
    elif session.twin is not None:
        try:
            lai = session.twin.field.state().get("LAI")
        except Exception:  # noqa: BLE001 -- no twin state: no canopy prediction
            lai = None
    return float(1.0 - np.exp(-K_COVER * float(lai))) if lai is not None else float("nan")


def plan_weather(session, rng: np.random.Generator):
    """Flight weather for today's job, drawn from its own generator (never the season's)."""
    from fa_drone.mission import Weather

    from ..scenarios import mission_weather
    rec = session.world.weather(session.day)
    if rec is None:
        return Weather(wind_ms=1.0, wind_dir_deg=0.0, gust_ms=1.6, temp_c=20.0)
    return mission_weather(rec, rng)


def freeze_prediction(session, op) -> Prediction:
    """The plan for this job as it stands before take-off: per-cell weed map and spread from
    the twin, canopy cover, targets and the simulated open-loop flight (dose, sorties, time)."""
    from fa_drone.mission import MissionSettings, simulate_mission
    n = session.grid.n_cells
    flight = MissionSettings(**op.settings) if op.settings else session.flight
    seed = session.seed * 97 + len(session.missions_log)
    rng = np.random.default_rng(seed + 7_000_003)
    def sim(weather):
        return simulate_mission(session.grid, session.spec, flight, weather, targets=op.targets, seed=seed,
                                rules=session.region.rules, power=session.pm, spray=session.sm,
                                record_track=False)
    res = sim(plan_weather(session, rng))
    if not res.feasible:                      # not flyable today: the plan as designed, in calm air
        from fa_drone.mission import Weather
        res = sim(Weather(wind_ms=1.0, wind_dir_deg=0.0, gust_ms=1.6, temp_c=20.0))
    dig = session.twin.digital if session.twin is not None else None
    if dig is not None:
        wm = np.where(np.isfinite(dig.weed_map), dig.weed_map, 0.0)
        wsd = np.where(np.isfinite(dig.weed_map), dig.weed_map_sd, np.inf)
    else:
        wm, wsd = np.zeros(n), np.full(n, np.inf)
    targets = np.ones(n, bool) if op.targets is None else np.asarray(op.targets, bool)
    k = res.kpis if res.feasible else {"sorties": 0, "time_min": 0.0, "litres": 0.0}
    dose = res.dose_l_ha if res.feasible else np.where(targets, flight.rate_l_ha, 0.0)
    return Prediction(op.id, op.product, wm, wsd, _twin_cc(session), CC_SD, targets, np.asarray(dose, float),
                      int(k["sorties"]), float(k["time_min"]), float(k["litres"]),
                      session.day.isoformat() if isinstance(session.day, dt.date) else str(session.day))
