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


def _synthetic_records(latitude, longitude, start, days, seed, elevation):
    import numpy as np
    rng = np.random.default_rng(seed)
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        doy = d.timetuple().tm_yday
        season = np.sin(2 * np.pi * (doy - 80) / 365.25)
        tmean = 24.0 + 8.0 * season + rng.normal(0, 1.6)
        irrad = (1.05e7 + 5.5e6 * season) * (0.55 + 0.45 * rng.random())
        monsoon = 1.0 if 152 <= doy <= 273 else 0.12
        svp = 6.11 * 10 ** (7.5 * tmean / (237.3 + tmean))
        out.append(dict(
            DAY=d, LAT=latitude, LON=longitude, ELEV=elevation,
            IRRAD=float(max(2.0e6, irrad)),
            TMIN=float(tmean - 6.5), TMAX=float(tmean + 6.5),
            VAP=float(max(3.0, 0.55 * svp)),
            WIND=float(max(0.3, rng.gamma(2.0, 1.0))),
            RAIN=float(round(rng.gamma(0.6, 1.1) * monsoon, 3)),
        ))
    return out


def make_synthetic_weather(latitude: float, longitude: float, start: date,
                           days: int = 400, seed: int = 0, elevation: float = 250.0):
    """Deterministic synthetic weather, for tests and offline demos.

    This exists so the entire system is runnable with no network at all -- which
    matters both for CI and for a field gateway. It is not a climate model and
    must never back a real recommendation, so the provider stamps a loud
    description that the twin copies into every summary it produces. A synthetic
    result that cannot be distinguished from a real one is a trap.
    """
    from pcse.base import WeatherDataProvider, WeatherDataContainer

    class _Synthetic(WeatherDataProvider):
        def __init__(self):
            super().__init__()
            self.latitude, self.longitude, self.elevation = latitude, longitude, elevation
            self.angstA, self.angstB = 0.25, 0.5
            self.description = ["SYNTHETIC weather (deterministic, seeded) -- NOT for real advice"]
            from pcse.util import reference_ET
            for rec in _synthetic_records(latitude, longitude, start, days, seed, elevation):
                # Reference ET is computed with PCSE's own Penman-Monteith
                # routine rather than stubbed, so the synthetic water balance is
                # at least internally consistent with the driving variables.
                e0, es0, et0 = reference_ET(
                    DAY=rec["DAY"], LAT=latitude, ELEV=elevation,
                    TMIN=rec["TMIN"], TMAX=rec["TMAX"], IRRAD=rec["IRRAD"],
                    VAP=rec["VAP"], WIND=rec["WIND"],
                    ANGSTA=self.angstA, ANGSTB=self.angstB, ETMODEL="PM")
                rec = dict(rec, E0=e0 / 10.0, ES0=es0 / 10.0, ET0=et0 / 10.0)
                wdc = WeatherDataContainer(**rec)
                self._store_WeatherDataContainer(wdc, rec["DAY"])

        def __str__(self):
            return (f"SyntheticWeather(lat={latitude:.3f},lon={longitude:.3f},"
                    f"seed={seed}) NOT-FOR-ADVICE")

    return _Synthetic()


# Backwards-compatible alias.
SyntheticWeather = make_synthetic_weather
