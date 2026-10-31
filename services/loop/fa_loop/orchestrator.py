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
from fa_twin.weather import complete_members, records_provider, validate_records


class WeatherGap(ValueError):
    """Advice was asked for a day the twin has no observed weather to reach."""

K_COVER = 0.6

#: Crops for which a damage model and economics exist. Spray advice for any
#: other crop is refused rather than computed with potato's blight model.
DAMAGE_MODELLED = {"potato"}


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
    #: Risk-neutral ("mean") is the default. With the planner propagating blight
    #: state (31 Oct 2026, 40 paired seasons) it beat CVaR-80 by Rs 759/ha (95 % CI
    #: 390-1,112) with fewer sprays and equal yield. (A CVaR advantage measured
    #: earlier that day came from the planner bug that restarted the epidemic each
    #: week, and did not survive the fix.)
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
    #: Crops for which herbicide advice is given. The India loop kept potato only;
    #: the global runner passes its crop when the region pack prices it.
    herbicide_crops: frozenset = frozenset({"potato"})
    #: Expected dry yield (kg/ha) valuing a weed loss; None -> the potato ceiling.
    expected_yield_kg_ha: float | None = None


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
        probs = validate_records(observed_records)
        if probs:
            raise ValueError("observed weather rejected: " + "; ".join(probs))
        self.weather = records_provider(observed_records, cfg.latitude, cfg.longitude,
                                        description="observed weather (twin)")
        self.model = ZoneTwin(cfg, self.weather, engine=engine)
        self.last_irrigation: dt.date | None = None
        self.water_cm = 0.0
        #: (model day when applied, cm) -- replayed when the crop is rebuilt
        self.irrigation_log: list[tuple[dt.date, float]] = []
        self.sprays: list[dt.date] = []
        self.protected_until: dt.date | None = None
        self.flights = 0
        self.blight_detected = False
        #: every scouting flight: (day, severity) or (day, None) when nothing was seen
        self.scouting: list[tuple[dt.date, float | None]] = []
        self.herbicide: Any = None
        self._growth_cache: dict = {}
        self.damage_modelled = cfg.crop in DAMAGE_MODELLED

    # -- state ------------------------------------------------------------------
    @property
    def day(self) -> dt.date:
        return self.model.day

    def state(self) -> dict:
        return self.model.state()

    @property
    def records(self) -> list[dict]:
        """Observed daily weather the twin has received so far."""
        return self.weather.records

    @property
    def horizon(self) -> dt.date:
        return self.cfg.harvest_date + dt.timedelta(days=10)

    def add_weather(self, records: list[dict]) -> int:
        """Append observed days (continuing from the last one) and apply rain wash-off."""
        last = self.weather.last_day()
        recs = sorted(records, key=lambda r: r["DAY"])
        new = [r for r in recs if last is None or r["DAY"] > last]
        if new and last is not None and (new[0]["DAY"] - last).days != 1:
            raise WeatherGap(f"observed weather jumps from {last} to {new[0]['DAY']}; send the missing days")
        probs = validate_records(new)
        if probs:
            raise ValueError("observed weather rejected: " + "; ".join(probs))
        self.weather.extend(new)
        for r in new:
            self.record_rain_washoff(r)
        return len(new)

    def advance_to(self, day: dt.date) -> None:
        """Bring the crop up to date for decisions on ``day``.

        PCSE needs a day's weather to step *into* it, so with observed weather
        through ``day - 1`` the twin stands at the end of ``day - 1`` -- which is
        exactly the state a morning decision on ``day`` is made from (and where
        an irrigation signal enters the next day's water balance).
        """
        last = self.weather.last_day()
        if last is None or last < day - dt.timedelta(days=1):
            raise WeatherGap(f"no observed weather after {last}; the twin needs every day "
                             f"up to {day - dt.timedelta(days=1)} before advising on {day}")
        target = min(day, last)
        if target > self.model.day and not self.model.terminated:
            self.model.run_till(target)

    def planning_members(self, day: dt.date, forecast: list[list[dict]] | None, n: int,
                         seed: int) -> list[list[dict]]:
        """Observed past + forecast + climatology to harvest, one per member (CRN seeds)."""
        return complete_members(self.records, day, self.horizon, n, seed, forecast)

    # -- daily: irrigation ----------------------------------------------------
    def irrigation_advice(self, day: dt.date, forecast: list[list[dict]] | None) -> Decision:
        self.advance_to(day)
        forecast = forecast if forecast else self.planning_members(day, None, 3, seed=day.toordinal())
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
        self.irrigation_log.append((self.model.day, float(amount_cm)))
        self.last_irrigation = day
        self.water_cm += amount_cm

    def _irrigation_events(self) -> list[dict]:
        return [{"date": d, "irrigation_cm": a, "efficiency": self.s.irrigation_efficiency}
                for d, a in self.irrigation_log]

    # -- once: the camera pass --------------------------------------------------
    def ingest_pass(self, day: dt.date, canopy_cover: float, growth_stage_dvs: float | None = None,
                    weed_density_cells: np.ndarray | None = None, canopy_cover_sd: float = 0.04,
                    dvs_sd: float = 0.05, members: int = 16, seed: int = 0) -> Decision:
        """One field pass: canopy cover and growth stage are assimilated with the
        ensemble Kalman filter; a sampled weed-density map decides herbicide.

        A 16-member ensemble (TSUM1/TSUM2/SPAN/RGRLAI/TDWI spread 20 %) is run on
        the observed weather and irrigation to the pass day; cover goes in
        through its operator (1 - exp(-0.6 LAI), sd ``canopy_cover_sd``) and the
        stage as DVS (sd ``dvs_sd``). Members are re-run from sowing with their
        analysed parameters, so the stage observation corrects development, not
        just the leaf area. The deterministic twin that drives irrigation is then
        rebuilt with the posterior-mean parameters and set to the posterior-mean
        LAI. In simulation a pass near day 45 cut the 30-day canopy forecast
        error by about half and did not improve the yield forecast, so this
        updates the state the advice uses and makes no yield claim.
        """
        from fa_twin.assimilation import EnsembleTwin, Observation, ParameterPerturbation
        self.advance_to(day)
        when = self.model.day
        cc = float(np.clip(canopy_cover, 0.0, 0.985))
        lai_obs = -math.log(1.0 - cc) / K_COVER
        st = self.state()
        detail = []
        data: dict = dict(canopy_cover=cc, lai_obs=round(lai_obs, 3), dvs_obs=growth_stage_dvs)
        if st.get("LAI"):
            events = self._irrigation_events()
            pert = ParameterPerturbation(TSUM1=0.2, TSUM2=0.2, SPAN=0.2, RGRLAI=0.2, TDWI=0.2,
                                         WAV=0.0, NAVAILI=0.0)
            ens = EnsembleTwin(self.cfg, self.weather, n_members=members, perturbation=pert,
                               events=events, seed=seed, engine=self.engine)
            ens.run_till(when)
            obs = [Observation(when, "CC", cc, canopy_cover_sd)]
            if growth_stage_dvs is not None:
                obs.append(Observation(when, "DVS", float(growth_stage_dvs), dvs_sd))
            prior_lai = float(ens._augmented()[0].mean())
            _, rejected = ens.assimilate(obs)
            X = ens._augmented()
            post_lai, post_dvs = float(X[0].mean()), float(X[1].mean())
            params = ens.posterior_parameters()
            cfg = copy.deepcopy(self.cfg)
            cfg.param_overrides = {**(cfg.param_overrides or {}), **params}
            self.model = ZoneTwin(cfg, self.weather, events=events, engine=self.engine)
            self.model.run_till(when)
            if self.state().get("LAI"):
                self.model.set_lai(max(0.05, post_lai))
            detail.append(f"EnKF: LAI twin {st['LAI']:.2f} / ensemble {prior_lai:.2f} -> {post_lai:.2f} "
                          f"(cover {cc:.2f} implies {lai_obs:.2f})")
            if growth_stage_dvs is not None:
                detail.append(f"stage DVS obs {growth_stage_dvs:.2f}, twin {st.get('DVS', 0):.2f} -> {post_dvs:.2f}")
            if rejected:
                detail.append("an observation was rejected as a gross error (>5 sigma)")
            data.update(method="EnKF", lai_prior=round(st["LAI"], 3), lai_posterior=round(post_lai, 3),
                        dvs_posterior=round(post_dvs, 3), rejected=bool(rejected),
                        posterior_parameters={k: round(v, 4) for k, v in params.items()})
        else:
            detail.append("crop not emerged; pass recorded, no state update")
            data.update(method="none")
        if weed_density_cells is not None and self.cfg.crop not in self.s.herbicide_crops:
            msg = (f"herbicide advice not enabled for {self.cfg.crop}: no weed economics "
                   "configured for this crop")
            detail.append(msg)
            data["herbicide"] = "not modelled"
            self.log.append(Decision(day, "herbicide", False, msg))
        elif weed_density_cells is not None:
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
        return float(self.s.expected_yield_kg_ha) if self.s.expected_yield_kg_ha else 9000.0

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
        """A scouting result -- a negative one (None / below detection) is information too."""
        from fa_planner.weekly import DETECTION_LIMIT
        self.flights += 1
        seen = severity is not None and severity >= DETECTION_LIMIT
        self.scouting.append((day, float(severity) if seen else None))
        self.blight_detected = self.blight_detected or seen

    def weekly_plan(self, day: dt.date, forecast: list[list[dict]], seed: int = 0) -> dict:
        self.advance_to(day)
        status = FieldStatus(day=day, plant=self.cfg.sowing_date, harvest=self.cfg.harvest_date,
                             protected_until=self.protected_until, inoculum=self.inoculum,
                             scouting=list(self.scouting), sprays=list(self.sprays))
        plan: dict = {"day": day}
        if not self.damage_modelled:
            from fa_planner.weekly import FungicideDecision
            why = (f"fungicide advice not modelled for {self.cfg.crop}: the blight model and "
                   "spray economics are potato's")
            plan["scout"] = False
            plan["fungicide"] = FungicideDecision(None, 0.0, 0.0, {}, why)
            self.log.append(Decision(day, "fungicide", False, why))
            return plan
        members = self.planning_members(day, forecast, self.s.plan_members, seed)
        fd = fungicide_decision(members, status, self._growth_fn, self.s.econ, self.s.blight,
                                self.s.objective, seed=seed)
        if self.s.scouting:
            last = self.scouting[-1][0] if self.scouting else None
            scout, why = scouting_trigger(members, status, self.s.blight, last_scouted=last,
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
