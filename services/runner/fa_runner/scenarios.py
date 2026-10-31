"""Future scenarios for the twin's iterations.

``ForecastFeed`` gives the planner full-season members as an operational twin
would have them on the issue date: the observed past, the **real archived
ensemble forecast issued that day** (ECMWF IFS/AIFS ENS or NOAA GEFS, bias-
corrected on the fly against observed weather), and a tail of real analog years
from the site's own record. It also counts which forecast source was used so a
report can say how much of the run was driven by real forecasts.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

import numpy as np

from fa_farm.sources.base import Cache
from fa_farm.sources.forecasts import (BiasCorrector, ForecastSource, ensemble_forecast,
                                       season_members)
from fa_planner.sop import SprayWindow


class ForecastFeed:
    def __init__(self, lat: float, lon: float, history: list[dict], cache: Cache | None = None,
                 sources: list[ForecastSource] | None = None):
        self.lat, self.lon = lat, lon
        self.history = history
        self.cache = cache or Cache()
        self.sources = sources
        self.corrector = BiasCorrector()
        self.sources_used: Counter = Counter()
        self.provenance: dict = {}
        self._pending: list[dict] = []          # day 0-2 ensemble means awaiting observation

    def members(self, issue: dt.date, observed: list[dict], end: dt.date, n: int,
                seed: int = 0) -> list[list[dict]]:
        if self._pending:
            self.corrector.update(self._pending, observed)
            self._pending = [p for p in self._pending if p["DAY"] >= issue]
        fc, src, prov = ensemble_forecast(self.lat, self.lon, issue, self.cache, self.history,
                                          sources=self.sources, n_members=max(n, 2),
                                          corrector=self.corrector, seed=seed)
        self.sources_used[src.value] += 1
        self.provenance[src.value] = prov
        if src is not ForecastSource.CLIMATOLOGY:
            for d in range(min(3, len(fc[0]))):
                day_recs = [m[d] for m in fc]
                self._pending.append({k: (float(np.mean([r[k] for r in day_recs])) if k != "DAY"
                                          else day_recs[0]["DAY"])
                                      for k in ("DAY", "TMAX", "TMIN", "RAIN", "IRRAD", "VAP")})
        full = season_members(fc[:n] if len(fc) >= n else (fc * (n // len(fc) + 1))[:n],
                              self.history, end, seed=seed)
        past = [dict(r) for r in observed if r["DAY"] < issue]
        out = []
        for m in full:
            mem = past + [r for r in m if r["DAY"] >= issue]
            out.append(mem)
        return out


#: Drones spray at dawn or dusk, when wind is lowest; daily-mean 2 m wind overstates it.
#: Ratio of early-morning to daily-mean wind (assumption ~0.7; replace with hourly
#: data where available).
DAWN_WIND_FACTOR = 0.7


def flight_record(rec: dict) -> dict:
    """The daily record as the spray crew meets it at dawn (wind scaled)."""
    return dict(rec, WIND=float(rec["WIND"]) * DAWN_WIND_FACTOR)


def spray_window(region, spec) -> SprayWindow:
    """The stricter of the region's label/regulatory wind limit and the aircraft's."""
    return SprayWindow(max_rain_mm=1.0,
                       max_wind_ms=min(region.rules.wind_max_ms, spec.fc.wind_continuous_ms),
                       max_tmax_c=min(35.0, spec.temp_range_c[1]))


def mission_weather(rec: dict, rng: np.random.Generator, gust_factor: float = 1.6):
    """Flight-time weather from a daily record: 2 m wind (the drone works at 2-4 m),
    gusts as a factor of the mean (assumption), direction drawn (daily direction is
    not in the record), mean temperature."""
    from fa_drone.mission import Weather
    w = float(rec["WIND"]) * DAWN_WIND_FACTOR
    return Weather(wind_ms=w, wind_dir_deg=float(rng.uniform(0, 360)),
                   gust_ms=w * gust_factor, temp_c=0.5 * (rec["TMAX"] + rec["TMIN"]))


def wind_samples(history: list[dict], months: set[int], window: SprayWindow, n: int,
                 seed: int = 0) -> list[tuple[float, float, float, float]]:
    """Flight-weather samples (wind, dir, gust, temp) from historical days in the
    season's months that pass the spray window -- the conditions a job will
    actually be flown in."""
    rng = np.random.default_rng(seed)
    ok = [flight_record(r) for r in history if r["DAY"].month in months and window.ok(flight_record(r))]
    if not ok:
        return []
    pick = rng.choice(len(ok), size=min(n, len(ok)), replace=False)
    out = []
    for i in pick:
        r = ok[int(i)]
        w = float(r["WIND"])
        out.append((w, float(rng.uniform(0, 360)), 1.6 * w, 0.5 * (r["TMAX"] + r["TMIN"])))
    return out
