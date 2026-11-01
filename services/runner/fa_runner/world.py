"""What the season loop runs against.

A ``World`` is the field as the operator meets it: observed weather day by day,
scans, and the place sprays land. ``VirtualWorld`` is a virtual test farm with a
hidden ground truth (so a season can be scored); ``RealWorld`` (added for live
fields) has observed data and uploads only, and no truth.
"""
from __future__ import annotations

import datetime as dt
from typing import Protocol

import numpy as np

from fa_farm.truth import AsApplied, GroundTruthFarm
from fa_scan.measure import simulate_scan
from fa_scan.survey import plan_scan


class WaitingForData(RuntimeError):
    """The session cannot advance: the next observation has not arrived yet."""


class World(Protocol):
    grid: object
    sowing: dt.date
    harvest: dt.date
    blight_modelled: bool

    def weather(self, d: dt.date) -> dict | None: ...
    def last_observed(self) -> dt.date: ...
    def pre_sowing(self) -> list[dict]: ...
    def advance_to(self, d: dt.date) -> None: ...
    def scan(self, d: dt.date, gsd_m: float, camera: str, rng: np.random.Generator,
             crop: str): ...
    def apply(self, aa: AsApplied) -> None: ...
    def truth_state(self): ...
    def harvest_result(self): ...
    def inoculum_alert(self) -> bool | None: ...


class VirtualWorld:
    """A virtual test farm: the ground truth built from real data, re-seeded per replicate."""

    has_truth = True

    def __init__(self, vf, seed: int = 0):
        spec = vf.spec.__class__(**{**vf.spec.__dict__, "seed": vf.spec.seed * 1000 + seed})
        self.truth = GroundTruthFarm(spec, vf.season_weather)
        self.grid = self.truth.grid
        self.sowing = self.truth.sowing
        self.harvest = self.truth.harvest_date
        self.blight_modelled = self.truth.blight_modelled
        self._records = vf.season_weather
        self._by_day = {r["DAY"]: r for r in vf.season_weather}

    def weather(self, d: dt.date) -> dict | None:
        return self._by_day.get(d)

    def last_observed(self) -> dt.date:
        return self._records[-1]["DAY"]

    def pre_sowing(self) -> list[dict]:
        first = self._records[0]["DAY"]
        return [r for r in self._records if first <= r["DAY"] < self.sowing]

    def advance_to(self, d: dt.date) -> None:
        self.truth.advance_to(d)

    def scan(self, d: dt.date, gsd_m: float, camera: str, rng: np.random.Generator, crop: str):
        state = self.truth.state()
        plan = plan_scan(self.grid, camera, gsd_m)
        return simulate_scan(state, self.grid, plan, rng, crop=crop,
                             days_after_sowing=(d - self.sowing).days)

    def apply(self, aa: AsApplied) -> None:
        self.truth.apply(aa)

    def truth_state(self):
        return self.truth.state()

    def harvest_result(self):
        return self.truth.harvest()

    def inoculum_alert(self) -> bool | None:
        return self.truth.blight_infection_day is not None
