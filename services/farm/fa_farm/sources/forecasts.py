"""Real, archived ensemble weather forecasts for any point on Earth.

The twin's "future iterations" must be driven by forecasts that were actually
issued, not by a generator, or a hindcast measures the generator. dynamical.org
republishes the operational archives as cloud-native Zarr (CC-BY-4.0):

* ECMWF IFS ENS, 0.25 deg, 51 members, 15 days -- from 2024-04-01
* ECMWF AIFS ENS (machine-learning ensemble), 51 members -- from 2025-07-02
* NOAA GEFS, 31 members, 35 days -- from 2020-10-01

Before an archive starts (or when a read fails) the forecast falls back to
**analog years** from the site's own observed history, and the provenance says
so. Beyond the forecast horizon, season members are completed with analog years
too, so every member stays real weather, just not a forecast.
"""
from __future__ import annotations

import datetime as dt
import functools
import io
import math
from enum import Enum

import numpy as np

from .base import Cache, DataUnavailable, Provenance, today
from .weather import es_hpa

U10_TO_U2 = 4.87 / math.log(67.8 * 10.0 - 5.42)     # FAO-56 log profile, 10 m -> 2 m


class ForecastSource(str, Enum):
    IFS_ENS = "ecmwf-ifs-ens"
    AIFS_ENS = "ecmwf-aifs-ens"
    GEFS = "noaa-gefs"
    CLIMATOLOGY = "climatology-analogs"


_META = {
    ForecastSource.IFS_ENS: dict(
        start=dt.date(2024, 4, 1), bucket="dynamical-ecmwf-ifs-ens",
        prefix="ecmwf-ifs-ens-forecast-15-day-0-25-degree/v0.1.0.icechunk",
        name="ECMWF IFS ENS 0.25 deg (via dynamical.org)",
        attrib="ECMWF IFS ENS forecast data processed by dynamical.org; ECMWF open data, CC-BY-4.0"),
    ForecastSource.AIFS_ENS: dict(
        start=dt.date(2025, 7, 2), bucket="dynamical-ecmwf-aifs-ens",
        prefix="ecmwf-aifs-ens-forecast/v0.1.0.icechunk",
        name="ECMWF AIFS ENS (via dynamical.org)",
        attrib="ECMWF AIFS ENS forecast data processed by dynamical.org; ECMWF open data, CC-BY-4.0"),
    ForecastSource.GEFS: dict(
        start=dt.date(2020, 10, 1), bucket="dynamical-noaa-gefs",
        prefix="noaa-gefs-forecast-35-day/v0.2.0.icechunk",
        name="NOAA GEFS 35 day (via dynamical.org)",
        attrib="NOAA GEFS forecast data processed by dynamical.org, CC-BY-4.0"),
}

_VARS = {
    ForecastSource.IFS_ENS: ("temperature_2m", "precipitation_surface",
                             "downward_short_wave_radiation_flux_surface",
                             "dew_point_temperature_2m", "wind_u_10m", "wind_v_10m"),
    ForecastSource.AIFS_ENS: ("temperature_2m", "precipitation_surface",
                              "downward_short_wave_radiation_flux_surface",
                              "dew_point_temperature_2m", "wind_u_10m", "wind_v_10m"),
    ForecastSource.GEFS: ("temperature_2m", "maximum_temperature_2m", "minimum_temperature_2m",
                          "precipitation_surface", "downward_short_wave_radiation_flux_surface",
                          "relative_humidity_2m", "wind_u_10m", "wind_v_10m"),
}


def available_sources(issue: dt.date) -> list[ForecastSource]:
    out = [s for s in (ForecastSource.IFS_ENS, ForecastSource.AIFS_ENS, ForecastSource.GEFS)
           if issue >= _META[s]["start"]]
    return out or [ForecastSource.CLIMATOLOGY]


@functools.lru_cache(maxsize=4)
def _dataset(source: ForecastSource):
    import icechunk
    import xarray as xr
    m = _META[source]
    st = icechunk.s3_storage(bucket=m["bucket"], prefix=m["prefix"], region="us-west-2",
                             anonymous=True)
    repo = icechunk.Repository.open(st)
    return xr.open_zarr(repo.readonly_session("main").store, chunks=None)


def read_point(source: ForecastSource, issue: dt.date, lat: float, lon: float,
               cache: Cache, max_lead_h: float = 15 * 24) -> dict[str, np.ndarray]:
    """Raw member arrays (lead, member) at the nearest grid point, cached."""
    key = cache.key("fc", source.value, issue.isoformat(), round(lat, 2), round(lon, 2), max_lead_h)
    blob = cache.get_bytes(key + ".npz")
    if blob is not None:
        z = np.load(io.BytesIO(blob))
        return {k: z[k] for k in z.files}
    try:
        ds = _dataset(source)
        pt = ds.sel(init_time=np.datetime64(issue.isoformat() + "T00"), latitude=lat,
                    longitude=lon, method="nearest")
        lead_h = pt.lead_time.values / np.timedelta64(1, "h")
        sel = lead_h <= max_lead_h + 1e-6
        pt = pt.isel(lead_time=np.where(sel)[0])
        arrs = {v: pt[v].load().values.astype("float32") for v in _VARS[source]}
        arrs["lead_hours"] = lead_h[sel].astype("float32")
    except Exception as e:  # noqa: BLE001 -- any archive failure is a missing layer
        raise DataUnavailable(f"forecast {source.value}", str(e)) from e
    if not np.isfinite(arrs[_VARS[source][0]]).any():
        raise DataUnavailable(f"forecast {source.value}", f"no data for init {issue}")
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrs)
    cache.put_bytes(key + ".npz", buf.getvalue())
    return arrs


def daily_members(arrs: dict[str, np.ndarray], issue: dt.date, source: ForecastSource,
                  days: int = 15) -> list[list[dict]]:
    """Aggregate step data to daily records (UTC days) per member."""
    lead = arrs["lead_hours"].astype(float)
    n_mem = arrs[_VARS[source][0]].shape[1]
    out = []
    for m in range(n_mem):
        recs = []
        for d in range(days):
            lo, hi = 24.0 * d, 24.0 * (d + 1)
            inst = (lead >= lo) & (lead <= hi)            # instantaneous samples in the day
            # step-averaged values: step i covers (lead[i-1], lead[i]]
            acc_idx = [i for i in range(1, len(lead)) if lead[i] > lo and lead[i] <= hi]
            if not inst.any() or not acc_idx:
                break
            dt_s = np.array([(lead[i] - lead[i - 1]) * 3600.0 for i in acc_idx])
            covered = dt_s.sum()
            if covered < 0.75 * 86400:
                break
            scale = 86400.0 / covered
            pr = arrs["precipitation_surface"][acc_idx, m]
            sw = arrs["downward_short_wave_radiation_flux_surface"][acc_idx, m]
            if not (np.isfinite(pr).all() and np.isfinite(sw).all()):
                break
            rain = float(np.nansum(pr * dt_s) * scale)
            irrad = float(np.nansum(sw * dt_s) * scale)
            t = arrs["temperature_2m"][inst, m]
            if source is ForecastSource.GEFS:
                tx = arrs["maximum_temperature_2m"][acc_idx, m]
                tn = arrs["minimum_temperature_2m"][acc_idx, m]
                tmax, tmin = float(np.nanmax(np.r_[tx, t])), float(np.nanmin(np.r_[tn, t]))
                rh = arrs["relative_humidity_2m"][inst, m]
                vap = float(np.nanmean(rh / 100.0 * np.array([es_hpa(x) for x in t])))
                rh_mean = float(np.nanmean(rh))
            else:
                tmax, tmin = float(np.nanmax(t)), float(np.nanmin(t))
                td = arrs["dew_point_temperature_2m"][inst, m]
                vap = float(np.nanmean([es_hpa(x) for x in td]))
                rh_mean = float(np.nanmean([100 * es_hpa(a) / es_hpa(b) for a, b in zip(td, t)]))
            u = arrs["wind_u_10m"][inst, m]
            v = arrs["wind_v_10m"][inst, m]
            wind = float(np.nanmean(np.hypot(u, v)) * U10_TO_U2)
            recs.append({"DAY": issue + dt.timedelta(days=d), "TMAX": tmax, "TMIN": tmin,
                         "RAIN": max(0.0, rain), "IRRAD": max(1e5, irrad), "VAP": vap,
                         "WIND": max(0.1, wind), "RH": min(100.0, rh_mean),
                         "WIND10_MAX": float(np.nanmax(np.hypot(u, v)))})
        out.append(recs)
    return out


class BiasCorrector:
    """Rolling forecast-vs-observed correction at the site: additive for
    temperatures and vapour pressure, multiplicative for rain and radiation.

    Correcting to *climatology* would erase the forecast's anomaly -- the very
    signal the planner needs -- so pairs of (forecast day-0..2, observed) are
    accumulated as the season goes and the systematic part is removed."""

    ADD = ("TMAX", "TMIN", "VAP")
    MUL = ("RAIN", "IRRAD")

    def __init__(self):
        self.pairs: list[tuple[dict, dict]] = []

    def update(self, forecast: list[dict], observed: list[dict]) -> None:
        obs = {r["DAY"]: r for r in observed}
        self.pairs += [(f, obs[f["DAY"]]) for f in forecast if f["DAY"] in obs]

    def factors(self) -> dict[str, float]:
        if len(self.pairs) < 5:
            return {}
        f = {}
        for k in self.ADD:
            f[k] = float(np.mean([o[k] - p[k] for p, o in self.pairs]))
        for k in self.MUL:
            sp, so = sum(p[k] for p, _ in self.pairs), sum(o[k] for _, o in self.pairs)
            f[k] = float(np.clip(so / sp, 0.5, 2.0)) if sp > 0 else 1.0
        return f

    def apply(self, members: list[list[dict]]) -> list[list[dict]]:
        f = self.factors()
        if not f:
            return members
        out = []
        for mem in members:
            new = []
            for r in mem:
                r = dict(r)
                for k in self.ADD:
                    r[k] = r[k] + f[k]
                for k in self.MUL:
                    r[k] = r[k] * f[k]
                if r["TMIN"] > r["TMAX"]:
                    r["TMIN"], r["TMAX"] = r["TMAX"], r["TMIN"]
                new.append(r)
            out.append(new)
        return out


def _analog(history: list[dict], start: dt.date, days: int, year: int) -> list[dict] | None:
    by_day = {r["DAY"]: r for r in history}
    out = []
    for i in range(days):
        target = start + dt.timedelta(days=i)
        try:
            src_day = target.replace(year=year + (target.year - start.year))
        except ValueError:                      # 29 Feb -> 28 Feb
            src_day = (target - dt.timedelta(days=1)).replace(year=year + (target.year - start.year))
        r = by_day.get(src_day)
        if r is None:
            return None
        out.append(dict(r, DAY=target))
    return out


def _years(history: list[dict]) -> list[int]:
    return sorted({r["DAY"].year for r in history})


def climatology_members(history: list[dict], start: dt.date, days: int, n: int,
                        seed: int = 0, exclude_year: int | None = None) -> list[list[dict]]:
    """Analog-year members: the same calendar days from past years of the site's
    own observed record, re-dated to the target dates."""
    rng = np.random.default_rng(seed)
    years = [y for y in _years(history) if y != exclude_year]
    cands = [a for y in years if (a := _analog(history, start, days, y)) is not None]
    if not cands:
        raise DataUnavailable("climatology analogs", f"history does not cover {start}+{days}d")
    idx = rng.choice(len(cands), size=n, replace=len(cands) < n)
    return [cands[i] for i in idx]


def ensemble_forecast(lat: float, lon: float, issue: dt.date, cache: Cache,
                      history: list[dict] | None = None, days: int = 15,
                      sources: list[ForecastSource] | None = None, n_members: int | None = None,
                      corrector: BiasCorrector | None = None, seed: int = 0
                      ) -> tuple[list[list[dict]], ForecastSource, Provenance]:
    """Daily ensemble for ``days`` from ``issue``: the best available real archive,
    else analog years from ``history``. Provenance names what was used."""
    errors = []
    for src in (sources or available_sources(issue)):
        if src is ForecastSource.CLIMATOLOGY:
            break
        try:
            arrs = read_point(src, issue, lat, lon, cache)
            mem = [m for m in daily_members(arrs, issue, src, days) if len(m) == days]
            if not mem:
                raise DataUnavailable(f"forecast {src.value}", "fewer days than requested")
            if n_members:
                mem = mem[:n_members]
            if corrector is not None:
                mem = corrector.apply(mem)
            m = _META[src]
            return mem, src, Provenance(m["name"], f"s3://{m['bucket']}/{m['prefix']}",
                                        "CC-BY-4.0", today(), m["attrib"], True,
                                        f"init {issue.isoformat()} 00 UTC")
        except DataUnavailable as e:
            errors.append(str(e))
    if not history:
        raise DataUnavailable("forecast", "; ".join(errors) or "no archive and no history")
    mem = climatology_members(history, issue, days, n_members or 20, seed=seed,
                              exclude_year=issue.year)
    note = "no archived forecast for this date" + (f" ({'; '.join(errors)})" if errors else "")
    return mem, ForecastSource.CLIMATOLOGY, Provenance(
        "Climatology analog years from the site's NASA POWER record (not a forecast)",
        "https://power.larc.nasa.gov", "NASA open data", today(),
        "NASA LaRC POWER", True, note)


def season_members(forecast: list[list[dict]], history: list[dict], end: dt.date,
                   seed: int = 0) -> list[list[dict]]:
    """Complete each forecast member to ``end`` with an analog-year tail."""
    out = []
    for i, mem in enumerate(forecast):
        last = mem[-1]["DAY"]
        n_tail = (end - last).days
        if n_tail <= 0:
            out.append([r for r in mem if r["DAY"] <= end])
            continue
        tail = climatology_members(history, last + dt.timedelta(days=1), n_tail, 1,
                                   seed=seed * 1000 + i, exclude_year=last.year)[0]
        out.append(list(mem) + tail)
    return out


def _ttl_cache(seconds: float):
    """Memoise per argument tuple for ``seconds`` (the archive grows once a day)."""
    import time

    def deco(fn):
        store: dict = {}

        @functools.wraps(fn)
        def wrapper(*args):
            hit = store.get(args)
            if hit is not None and time.time() - hit[0] < seconds:
                return hit[1]
            val = fn(*args)
            store[args] = (time.time(), val)
            return val

        wrapper.cache_clear = store.clear
        return wrapper
    return deco


@_ttl_cache(3600.0)
def latest_issue(source: ForecastSource) -> dt.date:
    """Newest 00 UTC initialisation in the archive. The repository is re-opened (at
    most hourly) because an open session does not see issues appended later."""
    if hasattr(_dataset, "cache_clear"):
        _dataset.cache_clear()
    try:
        t = np.asarray(_dataset(source).init_time.values).max()
    except Exception as e:  # noqa: BLE001
        raise DataUnavailable(f"forecast {source.value}", str(e)) from e
    return dt.date.fromisoformat(str(np.datetime_as_string(t, unit="D")))


def latest_forecast(lat: float, lon: float, cache: Cache, n_members: int = 20,
                    sources: list[ForecastSource] | None = None
                    ) -> tuple[list[list[dict]], ForecastSource, dt.date, Provenance]:
    """The most recent real ensemble forecast at a point (no climatology fallback:
    the caller decides how to label a missing forecast)."""
    errors = []
    for src in (sources or (ForecastSource.IFS_ENS, ForecastSource.GEFS, ForecastSource.AIFS_ENS)):
        try:
            issue = latest_issue(src)
            mem, used, prov = ensemble_forecast(lat, lon, issue, cache, None, sources=[src],
                                                n_members=n_members)
            return mem, used, issue, prov
        except DataUnavailable as e:
            errors.append(str(e))
    raise DataUnavailable("forecast", "; ".join(errors) or "no forecast source reachable")
