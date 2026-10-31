"""The field twin as an operational object.

One ``FieldTwin`` per field. It holds the crop model kept in step with the
field, and answers three questions on the schedule the audit found to matter:

* **daily** -- irrigate today? (soil-water trigger + 2-day rain forecast)
* **weekly** -- spray fungicide this week, scout, or wait? (forecast ensemble,
  common random numbers, Rs/ha)
* **once per season** -- ingest the one camera pass: canopy cover and growth
  stage reset the crop state; the weed-density map decides herbicide.

It does not know whether it is running on a real field or a simulated one.
``fa_loop.season`` drives it against a simulated truth; the API drives it from
real observations. That is deliberate: the code that is benchmarked is the code
that would advise a farmer.

Every decision is appended to ``log`` with its reason, so "why did it say
that?" has an answer months later.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from fa_planner.irrigation import IrrigationScheduler
from fa_planner.weekly import (FieldStatus, PotatoEconomics, fungicide_decision,
                               herbicide_decision, scouting_trigger)
from fa_twin.damage import BlightParams, WeedParams
from fa_twin.engine import ZoneConfig, ZoneTwin, check_photoperiod
from fa_twin.weather import records_provider

K_COVER = 0.6


@dataclass
class Decision:
    day: dt.date
    kind: str            # irrigate | fungicide | scout | herbicide | pass | gate | wait
    action: bool
    detail: str
    data: dict = field(default_factory=dict)


@dataclass
class TwinSettings:
    econ: PotatoEconomics = field(default_factory=PotatoEconomics)
    blight: BlightParams = field(default_factory=BlightParams)
    weeds: WeedParams = field(default_factory=WeedParams)
    irrigation: IrrigationScheduler = field(default_factory=IrrigationScheduler)
    objective: str = "mean"          # "mean" (risk-neutral) | "cvar"
    scouting: bool = True            # event-triggered scouting allowed
    #: Forecast members per weekly plan. Do not go below 10: with 4 members a
    #: simulated infected season got 4 sprays instead of 8 and lost 6.6 % yield
    #: (31 Oct 2026), because a small ensemble rarely contains the bad weather.
    plan_members: int = 10
    irrigation_efficiency: float = 0.7
    #: Fixed schedule used only inside the fungicide planning model to get a
    #: tuber-growth path per forecast member (the planner's job there is blight,
    #: not water).
    planning_irrigation_days: int = 10


class FieldTwin:
    """A crop twin for one field, advising on irrigation, fungicide, herbicide and scouting."""

    def __init__(self, cfg: ZoneConfig, observed_records: list[dict],
                 settings: TwinSettings | None = None, inoculum: bool | None = None,
                 engine: str = "Wofost72_WLP_CWB"):
        self.cfg = cfg
        self.s = settings or TwinSettings()
        self.inoculum = inoculum
        self.engine = engine
        self.log: list[Decision] = []
        gate = check_photoperiod(cfg, engine=engine)
        self.log.append(Decision(cfg.sowing_date, "gate", gate.ok,
                                 "feasible" if gate.ok else "; ".join(gate.reasons)))
        if not gate.ok:
            raise ValueError("feasibility gate refused this configuration: " + "; ".join(gate.reasons))
        self.records = observed_records
        self.weather = records_provider(observed_records, cfg.latitude, cfg.longitude,
                                        description="observed weather (twin)")
        self.model = ZoneTwin(cfg, self.weather, engine=engine)
        self.last_irrigation: dt.date | None = None
        self.water_cm = 0.0
        self.sprays: list[dt.date] = []
        self.protected_until: dt.date | None = None
        self.flights = 0
        self.blight_detected = False
        self.observed_severity: float | None = None
        self.herbicide: Any = None
        self._growth_cache: dict = {}

    # -- state ------------------------------------------------------------------
    @property
    def day(self) -> dt.date:
        return self.model.day

    def state(self) -> dict:
        return self.model.state()

    def advance_to(self, day: dt.date) -> None:
        if day > self.model.day and not self.model.terminated:
            self.model.run_till(day)

    # -- daily: irrigation ----------------------------------------------------
    def irrigation_advice(self, day: dt.date, forecast: list[list[dict]]) -> Decision:
        self.advance_to(day)
        sm = self.state().get("SM")
        soil = self.cfg.soil
        rain = self.s.irrigation.forecast_rain(forecast, day)
        dec = self.s.irrigation.decide(day, sm, soil.SMFCF, soil.SMW, self.last_irrigation, rain)
        d = Decision(day, "irrigate", dec.irrigate, dec.reason,
                     dict(amount_cm=dec.amount_cm, soil_moisture=sm, trigger=dec.trigger_level,
                          forecast_rain_mm=round(rain, 1)))
        self.log.append(d)
        return d

    def record_irrigation(self, day: dt.date, amount_cm: float) -> None:
        """The irrigation actually applied (as metered) enters the twin's water balance."""
        from pcse import signals
        self.model.model._send_signal(signal=signals.irrigate, amount=float(amount_cm),
                                      efficiency=self.s.irrigation_efficiency)
        self.last_irrigation = day
        self.water_cm += amount_cm

    # -- once: the camera pass --------------------------------------------------
    def ingest_pass(self, day: dt.date, canopy_cover: float, growth_stage_dvs: float | None = None,
                    weed_density_cells: np.ndarray | None = None) -> Decision:
        """One field pass: canopy cover (and growth stage) reset the crop state;
        a sampled weed-density map decides herbicide.

        Canopy cover is inverted to LAI with the same Beer-Lambert operator the
        ensemble filter uses. The audit found one pass at day 30-45 cuts the
        30-day canopy forecast error by a third to a half but does **not**
        improve the yield forecast, so this updates the state the irrigation
        and spray planning use and makes no yield claim.
        """
        self.advance_to(day)
        cc = float(np.clip(canopy_cover, 0.0, 0.985))
        lai_obs = -math.log(1.0 - cc) / K_COVER
        st = self.state()
        detail = []
        if st.get("LAI"):
            self.model.set_lai(max(0.05, lai_obs))
            detail.append(f"LAI {st['LAI']:.2f} -> {lai_obs:.2f} from cover {cc:.2f}")
        if growth_stage_dvs is not None and st.get("DVS") is not None:
            detail.append(f"stage obs DVS {growth_stage_dvs:.2f} vs twin {st['DVS']:.2f}")
        data: dict = dict(canopy_cover=cc, lai_obs=lai_obs, dvs_obs=growth_stage_dvs)
        if weed_density_cells is not None:
            exp_y = self._expected_yield_hint()
            self.herbicide = herbicide_decision(np.asarray(weed_density_cells), exp_y,
                                                self.s.econ, self.s.weeds)
            detail.append(self.herbicide.reason)
            data["herbicide"] = self.herbicide.option
            data["herbicide_treated_fraction"] = self.herbicide.treated_fraction
            self.log.append(Decision(day, "herbicide", self.herbicide.option != "none",
                                     self.herbicide.reason,
                                     dict(option=self.herbicide.option,
                                          treated_fraction=self.herbicide.treated_fraction,
                                          threshold_m2=self.herbicide.threshold_m2)))
        d = Decision(day, "pass", True, "; ".join(detail), data)
        self.log.append(d)
        return d

    def _expected_yield_hint(self) -> float:
        """Expected dry tuber yield at the model ceiling (kg/ha) for the herbicide economics.

        ~45 t/ha fresh x 20 % dry matter; ``PotatoEconomics.revenue`` applies the
        farm factor, so a weed loss is valued as a share of a farm-level crop.
        """
        return 9000.0

    # -- weekly: fungicide + scouting ---------------------------------------------
    def _growth_fn(self, recs: list[dict]) -> dict:
        key = (recs[0]["DAY"], round(sum(r["RAIN"] for r in recs), 3),
               round(sum(r["TMAX"] for r in recs), 3))
        hit = self._growth_cache.get(key)
        if hit is not None:
            return hit
        cfg = copy.deepcopy(self.cfg)
        ev = [{"date": cfg.sowing_date + dt.timedelta(days=self.s.planning_irrigation_days * (i + 1)),
               "irrigation_cm": 4.0}
              for i in range(int((cfg.harvest_date - cfg.sowing_date).days
                                 / self.s.planning_irrigation_days))]
        tw = ZoneTwin(cfg, records_provider(recs, cfg.latitude, cfg.longitude), events=ev,
                      engine=self.engine)
        tw.run_to_end()
        df = tw.output_frame()
        tw_ = df["TWSO"].fillna(0.0)
        growth = {d: max(0.0, float(v - p)) for d, v, p in
                  zip(tw_.index[1:], tw_.values[1:], tw_.values[:-1])}
        self._growth_cache[key] = growth
        return growth

    def record_spray(self, day: dt.date) -> None:
        self.sprays.append(day)
        self.protected_until = day + dt.timedelta(days=self.s.blight.protect_days)

    def record_rain_washoff(self, rec: dict) -> None:
        if self.protected_until and rec["RAIN"] >= self.s.blight.washoff_mm:
            self.protected_until = None

    def record_scouting(self, day: dt.date, severity: float | None) -> None:
        self.flights += 1
        if severity is not None and severity >= 0.005:
            self.blight_detected = True
            self.observed_severity = float(severity)

    def weekly_plan(self, day: dt.date, forecast: list[list[dict]], seed: int = 0) -> dict:
        self.advance_to(day)
        status = FieldStatus(day=day, plant=self.cfg.sowing_date, harvest=self.cfg.harvest_date,
                             observed_severity=self.observed_severity,
                             protected_until=self.protected_until, inoculum=self.inoculum)
        plan: dict = {"day": day}
        members = forecast[: self.s.plan_members]
        fd = fungicide_decision(members, status, self._growth_fn, self.s.econ, self.s.blight,
                                self.s.objective, seed=seed)
        if self.s.scouting:
            scout, why = scouting_trigger(forecast, status, self.s.blight,
                                          already_detected=self.blight_detected,
                                          p_spray_better=fd.p_spray_better if fd.values else None)
            plan["scout"] = scout
            self.log.append(Decision(day, "scout", scout, why))
        plan["fungicide"] = fd
        self.log.append(Decision(day, "fungicide", fd.spray_day is not None, fd.reason,
                                 dict(spray_day=str(fd.spray_day) if fd.spray_day else None,
                                      expected_gain=round(fd.expected_gain),
                                      p_spray_better=round(fd.p_spray_better, 2))))
        return plan

    def advice_log(self) -> list[dict]:
        return [dict(day=str(d.day), kind=d.kind, action=d.action, detail=d.detail, **d.data)
                for d in self.log]
