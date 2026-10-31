"""Irrigation scheduling from the twin's soil-water state.

This is the decision with the largest measured value in the whole system: in
the 30 Oct 2026 audit (S10, potato at Bennett, 20 seasons, the twin's soil
parameters wrong by +-10 %) it used **22.2 cm of water against the farmer's
40 cm (-44 %) at the same yield in 20/20 seasons**. A perfect soil sensor used
22.8 cm.

Read that number with its conditions: the simulated twin knew the past weather
exactly, WOFOST 7.2 does not penalise over-watering, and the baseline is a
fixed 4 cm every 10 days. In the field the twin's soil water will drift (ET
error, rain-gauge error, actual applied depth), so the field trial measures
this against flow meters and against a cheap soil-moisture sensor.

Rule (ported from ``audit/s10_irrigation.py``)
-----------------------------------------------
Irrigate ``amount_cm`` when the twin's root-zone soil moisture falls below
``wp + trigger * (fc - wp)`` (i.e. below 55 % of plant-available water by
default), at least ``min_interval_days`` after the last irrigation, unless the
forecast gives more than ``rain_skip_mm`` in the next ``lookahead_days``.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class IrrigationDecision:
    irrigate: bool
    amount_cm: float
    reason: str
    soil_moisture: float | None = None
    trigger_level: float | None = None
    forecast_rain_mm: float = 0.0


@dataclass
class IrrigationScheduler:
    trigger: float = 0.55             # fraction of plant-available water
    amount_cm: float = 4.0
    min_interval_days: int = 5
    rain_skip_mm: float = 10.0
    lookahead_days: int = 2

    def level(self, fc: float, wp: float) -> float:
        return wp + self.trigger * (fc - wp)

    def decide(self, day: dt.date, sm: float | None, fc: float, wp: float,
               last_irrigation: dt.date | None, forecast_rain_mm: float) -> IrrigationDecision:
        lvl = self.level(fc, wp)
        if sm is None:
            return IrrigationDecision(False, 0.0, "no soil-moisture state (crop not started)",
                                      None, lvl, forecast_rain_mm)
        if sm >= lvl:
            return IrrigationDecision(False, 0.0, f"soil moisture {sm:.3f} above trigger {lvl:.3f}",
                                      sm, lvl, forecast_rain_mm)
        if last_irrigation is not None and (day - last_irrigation).days < self.min_interval_days:
            return IrrigationDecision(False, 0.0,
                                      f"irrigated {(day - last_irrigation).days} d ago "
                                      f"(min {self.min_interval_days})", sm, lvl, forecast_rain_mm)
        if forecast_rain_mm > self.rain_skip_mm:
            return IrrigationDecision(False, 0.0,
                                      f"{forecast_rain_mm:.0f} mm rain forecast in next "
                                      f"{self.lookahead_days} d", sm, lvl, forecast_rain_mm)
        return IrrigationDecision(True, self.amount_cm,
                                  f"soil moisture {sm:.3f} below trigger {lvl:.3f}",
                                  sm, lvl, forecast_rain_mm)

    def forecast_rain(self, members: list[list[dict]], day: dt.date) -> float:
        """Ensemble-mean rain (mm) over the lookahead window after ``day``."""
        if not members:
            return 0.0
        tot = 0.0
        for m in members:
            by = {r["DAY"]: r for r in m}
            tot += sum(by[d]["RAIN"] for k in range(1, self.lookahead_days + 1)
                       if (d := day + dt.timedelta(days=k)) in by)
        return tot / len(members)
