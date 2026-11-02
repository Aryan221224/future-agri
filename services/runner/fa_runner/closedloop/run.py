"""A spray job flown as a closed loop, sortie by sortie (virtual farms).

Each iteration simulates the job for the current targets and keeps only its first sortie:
the cells that sortie sprays, the litres it uses, the time it takes. The camera on the
sprayer observes what that sortie flew over; the observations are merged into the weed
map, scored against the frozen prediction, and the rest of the job is re-planned. With a
spot valve the boom only opens where the camera sees weeds above the threshold (or did not
see at all: fail-safe, spray as planned), so a cell never gets more than the plan.

Each sortie is simulated as the job restricted to the targets the first sortie of the
remaining job reaches, so its deposition (including spill onto neighbouring cells), litres
and time are the drone twin's own. A cell still under 90 % of the rate after two passes is
dropped and audited (the boom cannot reach it), so the loop never re-flies it forever.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import numpy as np

from fa_drone.mission import MissionSettings, simulate_mission
from fa_scan.sprayer_camera import SprayerCamera, cells_along, observe

from .prediction import Deviation, Prediction, deviation_dose, deviation_weeds, freeze_prediction
from .replan import merge_observations, remaining_targets


@dataclass
class SortieReport:
    n: int
    cells_sprayed: int
    cells_seen: int
    litres: float
    time_min: float
    deviations: list[Deviation]
    correction: str
    remaining: int
    track: list[dict]
    detections: list[dict]
    targets_next: list[int]
    seen_cells: list[int] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"n": self.n, "cells_sprayed": self.cells_sprayed, "cells_seen": self.cells_seen,
                "litres": round(self.litres, 3), "time_min": round(self.time_min, 2),
                "deviations": [d.to_json() for d in self.deviations], "correction": self.correction,
                "remaining": self.remaining, "track": self.track, "detections": self.detections,
                "targets_next": self.targets_next, "seen_cells": self.seen_cells}


@dataclass
class ClosedLoopResult:
    prediction: Prediction
    sorties: list[SortieReport]
    dose_l_ha: np.ndarray
    kpis: dict
    valve: bool
    stopped: str
    audit: list[dict]
    warnings: list = field(default_factory=list)

    def summary(self) -> dict:
        p = self.prediction
        return {"valve": self.valve, "stopped": self.stopped,
                "planned": {"litres": round(p.litres, 2), "sorties": p.sorties, "time_min": round(p.time_min, 1)},
                "flown": {"litres": self.kpis["litres"], "sorties": self.kpis["sorties"],
                          "time_min": self.kpis["time_min"]},
                "sorties": [s.to_json() for s in self.sorties], "audit": self.audit,
                "prediction": p.to_json()}


MAX_PASSES = 2
MAX_SHARE = 1.25          # assumed label margin: a top-up never takes a cell past 125 % of the rate


def fly_closed_loop(session, op, valve: bool = False, camera: SprayerCamera | None = None,
                    max_sorties: int = 60, on_sortie=None, weather=None) -> ClosedLoopResult:
    from ..closedloop.prediction import plan_weather
    cam = camera or SprayerCamera()
    grid, spec = session.grid, session.spec
    n = grid.n_cells
    flight = MissionSettings(**op.settings) if op.settings else session.flight
    pred = freeze_prediction(session, op)
    seed = session.seed * 97 + len(session.missions_log)
    wx = weather or plan_weather(session, np.random.default_rng(seed + 7_000_003))
    cam_rng = np.random.default_rng(seed + 31_337)
    state = session.world.truth_state()
    das = (session.day - session.world.sowing).days
    gsd = cam.gsd_m(flight.height_m)
    herbicide = op.product == "herbicide"
    thr = session.twin.field.herbicide.threshold_m2 if (herbicide and session.twin is not None
                                                        and session.twin.field.herbicide is not None) else None
    weeds = pred.weeds_mean.copy()
    weeds_sd = pred.weeds_sd.copy()
    seen_cum = np.zeros(n, bool)
    dose_cum = np.zeros(n)
    treated = np.zeros(n, bool)
    targets = pred.targets.copy()
    reports: list[SortieReport] = []
    audit: list[dict] = []
    warnings: list[str] = []
    stopped, kpis0 = "max sorties", None
    passes = np.zeros(n, int)
    valve_done = np.zeros(n, bool)
    rate = flight.rate_l_ha

    def sim(tg, k, rate_k):
        fl = flight if rate_k == rate else dataclasses.replace(flight, rate_l_ha=rate_k)
        return simulate_mission(grid, spec, fl, wx, targets=tg, seed=seed + 1000 * (k - 1),
                                rules=session.region.rules, power=session.pm, spray=session.sm, record_track=True)
    for k in range(1, max_sorties + 1):
        fresh = targets & (dose_cum <= 0)
        if fresh.any():                                  # cells not sprayed yet: the full rate
            targets, rate_k = fresh, rate
        else:                                            # only under-dosed cells: top up, never far over
            rate_k = float(np.clip(np.median(rate - dose_cum[targets]), 0.25 * rate, rate))
            over = targets & (dose_cum + rate_k > MAX_SHARE * rate)
            if over.any():
                targets = targets & ~over
                audit.append({"sortie": k, "channel": "dose", "correction": "cells left as they are",
                              "reason": f"{int(over.sum())} under-dosed cells would pass {MAX_SHARE:.0%} of the "
                                        f"rate with a {rate_k:.1f} L/ha top-up"})
            if not targets.any():
                stopped = "done"
                break
        res = sim(targets, k, rate_k)
        if not res.feasible:
            stopped = "not flyable: " + "; ".join(res.reasons)
            break
        if res.kpis["sorties"] > 1:                     # fly only what the first sortie reaches
            first = cells_along(res.track, grid, flight.swath_m, sortie=1) & targets
            if not first.any():
                stopped = "no progress"
                audit.append({"sortie": k, "channel": "plan", "correction": "stopped: no progress",
                              "reason": "the next sortie would reach none of the remaining targets"})
                break
            res = sim(first, k, rate_k)
            if not res.feasible:
                stopped = "not flyable: " + "; ".join(res.reasons)
                break
            flown_targets = first
        else:
            flown_targets = targets
        kpis0 = kpis0 or dict(res.kpis)
        warnings += [w for w in res.warnings if w not in warnings]
        passes[flown_targets] += 1
        sprayed = res.dose_l_ha > 0
        seen = cells_along(res.track, grid, cam.footprint_m(flight.height_m))
        obs = observe(state, grid, seen, gsd, das, cam_rng)
        weeds, weeds_sd, seen_cum = merge_observations(weeds, weeds_sd, seen_cum, obs.weed_density_est,
                                                       obs.weed_density_sd, seen)
        dose = np.asarray(res.dose_l_ha, float).copy()
        litres, minutes = float(res.kpis["litres"]), float(res.kpis["time_min"])
        if valve and herbicide and thr is not None:
            informative = seen & np.isfinite(obs.weed_density_sd)
            open_ = ~informative | (weeds > thr)
            full = dose.sum()
            valve_done |= informative & ~open_          # deliberately not sprayed: done for this job
            dose = np.where(open_, dose, 0.0)
            litres *= dose.sum() / full if full > 0 else 0.0
        dose_cum += dose
        treated |= sprayed & (dose > 0)
        devs = [deviation_weeds(pred, obs.weed_density_est, obs.weed_density_sd, seen),
                deviation_dose(pred, dose_cum, sprayed)]
        new_targets, why = remaining_targets(session.twin, weeds, dose_cum, rate, op.product,
                                             original=pred.targets)
        new_targets &= ~valve_done
        given_up = new_targets & (passes >= MAX_PASSES)
        if given_up.any():
            new_targets &= ~given_up
            audit.append({"sortie": k, "channel": "dose", "correction": "cells dropped",
                          "reason": f"{int(given_up.sum())} cells still under 90 % of the rate after "
                                    f"{MAX_PASSES} passes: the boom cannot reach them at these settings"})
        sig = [d for d in devs if d.significant]
        if sig:
            correction = "; ".join(d.correction for d in sig)
        elif new_targets.any():
            correction = f"continue the plan: {int(new_targets.sum())} cells left"
        else:
            correction = "none needed"
        for d in sig:
            audit.append({"sortie": k, "channel": d.channel, "correction": d.correction,
                          "reason": f"{d.summary}; {why}"})
        det = [{"cell": int(i), "est": round(float(obs.weed_density_est[i]), 2)}
               for i in np.where(seen & np.isfinite(obs.weed_density_est))[0]]
        rep = SortieReport(k, int((dose > 0).sum()), int(seen.sum()), float(litres), float(minutes), devs,
                           correction, int(new_targets.sum()),
                           list(res.track), det,
                           np.where(new_targets)[0].tolist(), np.where(seen)[0].tolist())
        reports.append(rep)
        if on_sortie is not None:
            try:
                on_sortie(rep.to_json())
            except Exception:  # noqa: BLE001 -- a listener (view, job progress) never changes the flight
                pass
        if not new_targets.any():
            stopped = "done"
            audit.append({"sortie": k, "channel": "plan", "correction": "job complete", "reason": why})
            break
        targets = new_targets
    if not reports:
        raise ValueError(stopped)
    tm = treated if treated.any() else pred.targets
    d_t = dose_cum[tm]
    litres = float(sum(r.litres for r in reports))
    deposited = float(dose_cum.sum() * grid.cell_m ** 2 / 1e4)
    kpis = dict(kpis0)
    kpis.update(litres=round(litres, 2), sorties=len(reports), battery_swaps=len(reports) - 1,
                time_min=round(sum(r.time_min for r in reports), 2),
                cv_pct=round(100 * float(d_t.std() / d_t.mean()) if d_t.mean() > 0 else 100.0, 1),
                skipped_frac=round(float((d_t < 0.5 * rate).mean()), 4),
                overdosed_frac=round(float((d_t > 1.5 * rate).mean()), 4),
                off_target_frac=round(max(0.0, 1 - deposited / litres) if litres > 0 else 0.0, 4),
                area_ha=round(float(tm.sum() * grid.cell_m ** 2 / 1e4), 3))
    return ClosedLoopResult(pred, reports, dose_cum, kpis, bool(valve and herbicide), stopped, audit, warnings)
