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


class RealWorld:
    """A live field: observed weather as it is published, scans uploaded by the
    operator, flights recorded from the aircraft's job records. No truth, no scoring."""

    has_truth = False

    def __init__(self, ff, extra_weather: list[dict] | None = None, inoculum: bool | None = None):
        from fa_farm.archetypes import season_dates
        from fa_farm.grid import FarmGrid
        a = ff.spec.archetype
        self.ff = ff
        self.grid = FarmGrid.from_wgs84(ff.spec.polygon_wgs84, ff.spec.cell_m)
        self.sowing, self.harvest = season_dates(a, ff.spec.year)
        self.blight_modelled = a.crop == "potato" and "late_blight" in a.diseases
        self._by_day = {r["DAY"]: r for r in ff.observed}
        #: replay sees only the data that had arrived when each action was taken
        self.horizon: dt.date | None = None
        self.extend(extra_weather or [])
        self.applied: list[AsApplied] = []
        self._inoc = inoculum

    def extend(self, records: list[dict]) -> int:
        """Append newly published days (only those continuing the record)."""
        n = 0
        for r in sorted(records, key=lambda r: r["DAY"]):
            last = self._last()
            if r["DAY"] == last + dt.timedelta(days=1):
                self._by_day[r["DAY"]] = r
                n += 1
        return n

    def _last(self) -> dt.date:
        return max(self._by_day) if self._by_day else self.sowing - dt.timedelta(days=61)

    def weather(self, d: dt.date) -> dict | None:
        if self.horizon is not None and d > self.horizon:
            return None
        return self._by_day.get(d)

    def last_observed(self) -> dt.date:
        last = self._last()
        return min(last, self.horizon) if self.horizon is not None else last

    def pre_sowing(self) -> list[dict]:
        return [self._by_day[d] for d in sorted(self._by_day) if d < self.sowing]

    def advance_to(self, d: dt.date) -> None:
        pass

    def scan(self, d, gsd_m, camera, rng, crop):
        return None                         # a live scan arrives as an upload

    def apply(self, aa: AsApplied) -> None:
        self.applied.append(aa)

    def truth_state(self):
        return None

    def harvest_result(self):
        return None

    def inoculum_alert(self) -> bool | None:
        return self._inoc
