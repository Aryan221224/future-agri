"""Weather input for the crop model.

Design notes
------------
PCSE ships providers for NASA POWER and Open-Meteo. We default to Open-Meteo
(ERA5-Land, ~0.1 deg / 9 km) over NASA POWER (0.5 deg / ~55 km) because at 0.5
degrees a single grid cell spans multiple Indian districts, which is useless for
field-scale work.

Rainfall from any reanalysis is the dominant error term in kharif (monsoon)
simulations. We keep the provider pluggable specifically so a local gauge or IMD
gridded rainfall can be substituted for the reanalysis rainfall column without
touching anything downstream.

Everything is cached to disk. A field gateway must be able to run the twin with
no connectivity, and re-fetching a season of weather on every ensemble member
would be both slow and rude to the upstream API.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_CACHE = Path.home() / ".cache" / "futureagri" / "weather"


def _cache_path(cache_dir: Path, lat: float, lon: float, provider: str) -> Path:
    return cache_dir / f"{provider}_{lat:.4f}_{lon:.4f}.json"


def get_weather(latitude: float, longitude: float, provider: str = "openmeteo",
                cache_dir: Path | None = None, offline_ok: bool = True):
    """Return a PCSE WeatherDataProvider, cached to disk.

    Falls back through openmeteo -> nasapower -> cached copy, because a twin
    that refuses to run when one upstream API is down is not a twin anyone can
    depend on during a season.
    """
    cache_dir = Path(cache_dir or DEFAULT_CACHE)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = _cache_path(cache_dir, latitude, longitude, provider)

    order = [provider] + [p for p in ("openmeteo", "nasapower") if p != provider]
    for p in order:
        try:
            if p == "openmeteo":
                from pcse.input import OpenMeteoWeatherDataProvider as W
            elif p == "nasapower":
                from pcse.input import NASAPowerWeatherDataProvider as W
            else:
                raise ValueError(f"unknown weather provider {p!r}")
            wdp = W(latitude=latitude, longitude=longitude)
            try:
                wdp._dump(str(_cache_path(cache_dir, latitude, longitude, p)))
            except Exception:  # caching is best-effort, never fatal
                pass
            log.info("weather: using %s for (%.4f, %.4f)", p, latitude, longitude)
            return wdp
        except Exception as e:  # noqa: BLE001 - any upstream failure is equivalent here
            log.warning("weather provider %s unavailable: %s", p, e)

    if offline_ok:
        for p in order:
            c = _cache_path(cache_dir, latitude, longitude, p)
            if c.exists():
                from pcse.base import WeatherDataProvider
                log.warning("weather: falling back to cached %s", c.name)
                return WeatherDataProvider._load(str(c))
    raise RuntimeError(
        f"no weather available for ({latitude}, {longitude}): all providers failed "
        "and no cached copy exists"
    )


# ---------------------------------------------------------------------------
# Normals-calibrated stochastic weather (replaces the old sine generator)
# ---------------------------------------------------------------------------
#
# Why this replaced ``_synthetic_records``
# ----------------------------------------
# The 30 Oct 2026 audit found that every benchmark had run on a sine-wave
# generator producing **102 mm of rain a year** against ~775 mm at Delhi, with a
# fixed 13 C diurnal range. Water stress, blight-favourable days and the value
# of every irrigation decision depend on rain, so those headline numbers were
# not about Indian conditions at all. This generator reproduces the *statistics*
# of a real station (seasonality, monsoon, wet-day persistence, diurnal range,
# inter-annual variability). It still produces invented days, not history, so
# it keeps the NOT-FOR-ADVICE stamp.
#
# Station: New Delhi (Safdarjung). Tmax, Tmin, rain and rainy days (>= 2.5 mm)
# are the IMD 1991-2020 normals. Radiation, vapour pressure and wind are
# approximate values chosen to be physically consistent; replace them with
# station records when available. Bennett University (28.45 N, 77.58 E) is
# ~35 km from Safdarjung, close enough for statistics, not for day-to-day use.

#: month -> (Tmax C, Tmin C, rain mm, rainy days, radiation MJ/m2/d, VAP hPa, wind m/s)
DELHI_NORMALS: dict[int, tuple[float, ...]] = {
    1: (20.1, 7.5, 19.1, 1.7, 12.5, 10.0, 1.8),
    2: (24.2, 10.6, 21.3, 1.5, 15.8, 11.5, 2.1),
    3: (29.9, 15.6, 17.4, 1.7, 19.5, 13.5, 2.3),
    4: (36.5, 21.3, 16.3, 1.0, 22.6, 15.0, 2.5),
    5: (39.9, 25.8, 30.7, 2.7, 24.0, 19.0, 2.7),
    6: (39.0, 27.7, 74.1, 4.8, 22.0, 26.0, 2.7),
    7: (35.6, 27.5, 209.7, 9.7, 18.5, 32.0, 2.3),
    8: (34.2, 26.7, 233.1, 10.2, 17.5, 32.0, 2.0),
    9: (34.1, 25.0, 123.5, 5.5, 18.5, 28.0, 1.9),
    10: (33.0, 19.5, 15.1, 0.8, 17.5, 19.0, 1.6),
    11: (28.4, 13.0, 6.0, 0.4, 14.5, 13.0, 1.5),
    12: (22.8, 8.4, 8.1, 0.6, 12.0, 11.0, 1.6),
}
NORMALS_SOURCE = ("IMD New Delhi (Safdarjung) 1991-2020 normals for Tmax/Tmin/rain/rainy days; "
                  "radiation, vapour pressure and wind approximate")
_MDAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

#: Daily rain is capped. A gamma draw has an unbounded tail and an occasional
#: 400 mm day would dominate a season's water balance. Delhi's wettest days are
#: of order 150-250 mm, so 200 mm is a physical ceiling for a generator, not a
#: statement about extremes.
RAIN_CAP_MM = 200.0


def _smooth(day: date, idx: int, normals: dict) -> float:
    """Linear interpolation between mid-month normals (no steps at month edges)."""
    m = day.month
    mid = date(day.year, m, 15)
    if day >= mid:
        m2 = m % 12 + 1
        nxt = date(day.year + (1 if m == 12 else 0), m2, 15)
        w = (day - mid).days / (nxt - mid).days
        return normals[m][idx] * (1 - w) + normals[m2][idx] * w
    m0 = 12 if m == 1 else m - 1
    prv = date(day.year - (1 if m == 1 else 0), m0, 15)
    w = (day - prv).days / (mid - prv).days
    return normals[m0][idx] * (1 - w) + normals[m][idx] * w


def generate_records(start: date, days: int, seed: int, normals: dict | None = None,
                     anomaly: dict | None = None, rain_cap_mm: float = RAIN_CAP_MM) -> list[dict]:
    """Daily weather records with realistic statistics.

    Rain is a two-state Markov chain (wet-day persistence) with gamma amounts
    scaled so each month's expected total matches the normal. Temperatures carry
    AR(1) daily anomalies plus a season-level anomaly, and wet days are cooler by
    day and warmer by night. ``anomaly`` fixes the season-level shifts
    (``{"t": +C, "rain": factor}``); otherwise they are drawn, which gives
    inter-annual variability (dry years, wet years).

    Units: TMAX/TMIN C, RAIN mm/day, IRRAD J/m2/day, VAP hPa, WIND m/s.
    """
    import numpy as np
    nm = normals or DELHI_NORMALS
    rng = np.random.default_rng(seed)
    an = anomaly or {}
    t_an = float(an.get("t", rng.normal(0, 0.8)))
    rain_fac = float(an.get("rain", np.exp(rng.normal(0, 0.3))))
    ax = an_n = 0.0
    wet = False
    out: list[dict] = []
    for i in range(days):
        d = start + timedelta(days=i)
        m = d.month
        rain_m, rdays = nm[m][2], nm[m][3]
        nd = _MDAYS[m - 1]
        # Normals count days >= 2.5 mm; light-rain days roughly add 60 %.
        p_wet = min(0.9, rdays * 1.6 / nd)
        p_ww = min(0.95, p_wet + 0.35 * (1 - p_wet))
        p_dw = max(0.0, (p_wet - p_wet * p_ww) / max(1e-6, 1 - p_wet))
        wet = bool(rng.random() < (p_ww if wet else p_dw))
        rain = 0.0
        if wet and rain_m > 0:
            mean_wet = rain_m * rain_fac / max(0.5, p_wet * nd)
            rain = min(rain_cap_mm, float(rng.gamma(0.75, mean_wet / 0.75)))
        ax = 0.7 * ax + rng.normal(0, 1.6)
        an_n = 0.7 * an_n + rng.normal(0, 1.3)
        tmax = _smooth(d, 0, nm) + t_an + ax - (2.5 if wet else 0.0)
        tmin = _smooth(d, 1, nm) + t_an + 0.6 * an_n + (0.5 if wet else 0.0)
        tmin = min(tmin, tmax - 2.0)
        rad = _smooth(d, 4, nm) * (0.68 if wet else 1.06) * (1 + rng.normal(0, 0.06))
        vap = _smooth(d, 5, nm) * (1.12 if wet else 1.0) * (1 + rng.normal(0, 0.06))
        es_tmin = 6.108 * float(np.exp(17.27 * tmin / (tmin + 237.3)))
        vap = min(vap, 0.98 * es_tmin)   # vapour pressure cannot exceed saturation at Tmin
        wind = max(0.3, _smooth(d, 6, nm) * float(rng.gamma(4, 0.25)))
        out.append(dict(DAY=d, TMAX=float(tmax), TMIN=float(tmin), RAIN=float(rain),
                        IRRAD=float(max(3.0, rad) * 1e6), VAP=float(max(2.0, vap)),
                        WIND=float(wind)))
    return out


def records_provider(records: list[dict], latitude: float, longitude: float,
                     elevation: float = 200.0, description: str = "", soil_evap_factor: float = 1.0):
    """Wrap daily records (RAIN in mm) as a PCSE WeatherDataProvider.

    Reference ET is computed with PCSE's own Penman-Monteith so the water
    balance is consistent with the driving variables. The records stay
    attached as ``.records`` so forecasts and the damage layer can read the
    same days the crop model saw. ``soil_evap_factor`` scales the bare-soil
    evaporation demand (ES0) for surface residue (``fa_twin.management``).
    """
    from pcse.base import WeatherDataProvider, WeatherDataContainer
    from pcse.util import reference_ET

    desc = description or "stochastic weather"

    class _RecordsWeather(WeatherDataProvider):
        def __init__(self):
            super().__init__()
            self.latitude, self.longitude, self.elevation = latitude, longitude, elevation
            self.angstA, self.angstB = 0.25, 0.5
            self.description = [desc]
            self.records: list[dict] = []
            self.extend(records)

        def extend(self, new_records: list[dict]) -> None:
            """Add (or replace) days. A running crop model sees them immediately,
            which is how an operational twin receives each day's observed weather."""
            by_day = {r["DAY"]: i for i, r in enumerate(self.records)}
            for r in new_records:
                e0, es0, et0 = reference_ET(
                    DAY=r["DAY"], LAT=latitude, ELEV=elevation, TMIN=r["TMIN"],
                    TMAX=r["TMAX"], IRRAD=r["IRRAD"], VAP=r["VAP"], WIND=r["WIND"],
                    ANGSTA=0.25, ANGSTB=0.5, ETMODEL="PM")
                rec = dict(r, LAT=latitude, LON=longitude, ELEV=elevation,
                           E0=e0 / 10.0, ES0=es0 * soil_evap_factor / 10.0, ET0=et0 / 10.0,
                           RAIN=r["RAIN"] / 10.0)   # PCSE wants cm/day
                self._store_WeatherDataContainer(WeatherDataContainer(**rec), r["DAY"])
                if r["DAY"] in by_day:
                    self.records[by_day[r["DAY"]]] = dict(r)
                else:
                    self.records.append(dict(r))
            self.records.sort(key=lambda x: x["DAY"])

        def last_day(self) -> date | None:
            return self.records[-1]["DAY"] if self.records else None

        def __str__(self):
            return desc

    return _RecordsWeather()


def make_normals_weather(latitude: float, longitude: float, start: date, days: int = 400,
                         seed: int = 0, elevation: float = 200.0, anomaly: dict | None = None,
                         normals: dict | None = None):
    """Normals-calibrated stochastic weather as a PCSE provider (``.records`` attached)."""
    recs = generate_records(start, days, seed, normals=normals, anomaly=anomaly)
    desc = (f"STOCHASTIC weather calibrated to {NORMALS_SOURCE.split(' for ')[0]} "
            f"(lat={latitude:.3f},lon={longitude:.3f},seed={seed}) NOT-FOR-ADVICE")
    return records_provider(recs, latitude, longitude, elevation, desc)


def make_synthetic_weather(latitude: float, longitude: float, start: date,
                           days: int = 400, seed: int = 0, elevation: float = 250.0):
    """Deterministic stochastic weather for tests, demos and benchmarks.

    Kept under its old name so every caller moved to realistic statistics at
    once. It is still not a real weather record and must never back real
    advice, so the description stamps NOT-FOR-ADVICE and the twin copies that
    into every summary it produces.
    """
    return make_normals_weather(latitude, longitude, start, days, seed, elevation)


# Backwards-compatible alias.
SyntheticWeather = make_synthetic_weather


# ---------------------------------------------------------------------------
# Forecast ensemble: observed past + forecast (days 1-15) + climatology tail
# ---------------------------------------------------------------------------

def forecast_member(truth: list[dict], issue: date, seed: int, forecast_days: int = 15,
                    normals: dict | None = None) -> list[dict]:
    """One forecast-ensemble member issued on ``issue``.

    * before ``issue``: the observed record (the twin has seen the past);
    * lead 1..``forecast_days``: the truth plus error that grows with lead time,
      which is how a real NWP ensemble behaves (temperature sd 0.5*sqrt(lead) C,
      rain amounts mis-scaled and occasionally missed or invented);
    * beyond: an independent climatology realisation, because past ~2 weeks
      a forecast has no skill and pretending otherwise is overconfidence.

    In operation the forecast part comes from a real ensemble (Open-Meteo
    ensemble API); this function is the simulation stand-in that lets the
    planner be tested against a known truth.
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    clim = generate_records(truth[0]["DAY"], len(truth), seed + 7919, normals=normals)
    out = []
    for i, r in enumerate(truth):
        lead = (r["DAY"] - issue).days
        if lead < 0:
            out.append(dict(r))
        elif lead < forecast_days:
            e = float(rng.normal(0, 0.5 * np.sqrt(lead + 1)))
            if r["RAIN"] > 0:
                miss = rng.random() < min(0.5, 0.03 * lead)
                fac = float(np.exp(np.clip(rng.normal(0, 0.15 * np.sqrt(lead + 1)), -1, 1)))
                rain = 0.0 if miss else min(RAIN_CAP_MM, r["RAIN"] * fac)
            else:
                rain = float(rng.gamma(0.5, 2.0)) if rng.random() < 0.02 * lead else 0.0
            vap = r["VAP"] * (1 + float(rng.normal(0, 0.03 * (lead + 1))))
            tmax, tmin = r["TMAX"] + e, r["TMIN"] + e
            es_tmin = 6.108 * float(np.exp(17.27 * tmin / (tmin + 237.3)))
            out.append(dict(r, TMAX=tmax, TMIN=tmin, RAIN=rain,
                            VAP=float(max(2.0, min(vap, 0.98 * es_tmin)))))
        else:
            out.append(dict(clim[i], DAY=r["DAY"]))
    return out


def forecast_ensemble(truth: list[dict], issue: date, n_members: int, seed: int,
                      forecast_days: int = 15, normals: dict | None = None) -> list[list[dict]]:
    """``n_members`` forecast members issued on ``issue`` (see ``forecast_member``).

    Member ``j`` always uses seed ``seed*1000 + j``, so two candidate actions
    evaluated against the same ensemble see the *same* weather -- common random
    numbers, which the audit measured at 92.9 % decision agreement with a
    120-member reference against 85.8 % for independent draws.
    """
    return [forecast_member(truth, issue, seed * 1000 + j, forecast_days, normals)
            for j in range(n_members)]


def annual_totals(records: list[dict]) -> dict[str, float]:
    """Summary statistics used by the tests and the benchmark headers."""
    import numpy as np
    rain = np.array([r["RAIN"] for r in records])
    months = np.array([r["DAY"].month for r in records])
    years = len(records) / 365.25
    return {
        "rain_mm_per_year": float(rain.sum() / years),
        "jjas_mm_per_year": float(rain[np.isin(months, [6, 7, 8, 9])].sum() / years),
        "wet_days_per_year": float((rain >= 2.5).sum() / years),
        "max_daily_mm": float(rain.max()),
        "mean_dtr_c": float(np.mean([r["TMAX"] - r["TMIN"] for r in records])),
    }


# ---------------------------------------------------------------------------
# Operational members: observed past + real forecast + climatology to harvest
# ---------------------------------------------------------------------------

REQUIRED_KEYS = ("DAY", "TMAX", "TMIN", "RAIN", "IRRAD", "VAP", "WIND")


def validate_records(records: list[dict]) -> list[str]:
    """Problems with observed records: missing keys, gaps, implausible units."""
    probs = []
    for k in REQUIRED_KEYS:
        if records and k not in records[0]:
            probs.append(f"missing field {k}")
    if probs:
        return probs
    days = [r["DAY"] for r in records]
    for a, b in zip(days, days[1:]):
        if (b - a).days != 1:
            probs.append(f"gap or disorder between {a} and {b}")
            break
    for r in records:
        # Physical range (world records -89.2 C .. 56.7 C) -- this catches Kelvin / Fahrenheit and
        # swapped columns, not cold climates: a Nebraska gauge reads -12 C in February.
        if not (-70 <= r["TMIN"] <= r["TMAX"] <= 60):
            probs.append(f"{r['DAY']}: temperatures out of range (TMIN {r['TMIN']}, TMAX {r['TMAX']})")
            break
        if not (0 <= r["RAIN"] <= 600):
            probs.append(f"{r['DAY']}: RAIN {r['RAIN']} must be mm/day")
            break
        if not (1e5 <= r["IRRAD"] <= 4e7):
            probs.append(f"{r['DAY']}: IRRAD {r['IRRAD']} must be J/m2/day")
            break
    return probs


def compose_member(observed: list[dict], issue: date, end: date, seed: int,
                   forecast: list[dict] | None = None, normals: dict | None = None) -> list[dict]:
    """One planning member that is continuous from the first observed day to ``end``.

    * days before ``issue``: observed records (must be continuous up to issue-1);
    * days from ``issue``: the supplied forecast member, as far as it goes
      (a real 15-day NWP ensemble member, e.g. Open-Meteo);
    * the rest, to ``end``: a climatology realisation seeded by ``seed``.

    This is what lets the twin plan to harvest when the future is unknown --
    the operational path -- rather than only when a full season of weather is
    already in hand (the simulation path).
    """
    past = [dict(r) for r in observed if r["DAY"] < issue]
    fc = sorted((dict(r) for r in (forecast or []) if r["DAY"] >= issue), key=lambda r: r["DAY"])
    out = past + fc
    nxt = (out[-1]["DAY"] + timedelta(days=1)) if out else issue
    if nxt <= end:
        clim = generate_records(nxt, (end - nxt).days + 1, seed, normals=normals)
        out += clim
    return out


def complete_members(observed: list[dict], issue: date, end: date, n: int, seed: int,
                     forecast_members: list[list[dict]] | None = None,
                     normals: dict | None = None) -> list[list[dict]]:
    """``n`` planning members to ``end``. Members already spanning observed-start..end
    (simulation) are used as they are; short forecast members are completed with
    observed past and a climatology tail; with no forecast, members are
    observed past + climatology. Member j uses seed ``seed*1000 + j`` (CRN)."""
    first = observed[0]["DAY"] if observed else issue
    fms = forecast_members or []
    out = []
    for j in range(n):
        f = fms[j % len(fms)] if fms else None
        if f and f[0]["DAY"] <= first and f[-1]["DAY"] >= end:
            out.append(f)
        else:
            out.append(compose_member(observed, issue, end, seed * 1000 + j, f, normals))
    return out
