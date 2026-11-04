"""Ground weather stations (NOAA GHCN-Daily) on top of gridded weather.

Gridded products (NASA POWER, reanalysis + satellite) are smooth and can be ~1 C warm or miss
local storms; a rain gauge a few km away measures the field's own weather. GHCN-Daily has
~100 000 stations worldwide (India: several thousand IMD-contributed stations), served by NOAA
NCEI without a key. The nearest station within ``max_km`` with enough data replaces daily
TMAX / TMIN / RAIN; radiation, humidity and wind stay gridded (stations rarely measure them).

    merge_station(records, lat, lon, start, end, cache) -> (records, provenance | None, info)
"""
from __future__ import annotations

import datetime as dt
import math

from .base import Cache, DataUnavailable, Provenance, session, today

SEARCH = "https://www.ncei.noaa.gov/access/services/search/v1/data"
DATA = "https://www.ncei.noaa.gov/access/services/data/v1"
ATTRIB = "NOAA NCEI GHCN-Daily (Menne et al. 2012, doi:10.7289/V5D21VHZ)"


def _km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def nearest_stations(lat: float, lon: float, start: dt.date, end: dt.date, cache: Cache, max_km: float = 15.0) -> list:
    key = cache.key("ghcn-search", round(lat, 3), round(lon, 3), start.isoformat(), end.isoformat())
    hit = cache.get_json(key)
    if hit is None:
        d = max_km / 111.0
        r = session().get(SEARCH, params={"dataset": "daily-summaries", "dataTypes": "PRCP",
                                          "bbox": f"{lat + d},{lon - d / max(0.2, math.cos(math.radians(lat)))},"
                                                  f"{lat - d},{lon + d / max(0.2, math.cos(math.radians(lat)))}",
                                          "startDate": start.isoformat(), "endDate": end.isoformat(), "limit": 50},
                          timeout=45)
        r.raise_for_status()
        hit = []
        for res in r.json().get("results", []):
            st = (res.get("stations") or [{}])[0]
            lon_s, lat_s = (res.get("location") or {}).get("coordinates", [None, None])
            if lat_s is None:
                continue
            hit.append({"id": st.get("id"), "name": st.get("name"), "lat": lat_s, "lon": lon_s,
                        "start": res.get("startDate", "")[:10], "end": res.get("endDate", "")[:10]})
        cache.put_json(key, hit)
    out = [dict(s, km=round(_km(lat, lon, s["lat"], s["lon"]), 2)) for s in hit]
    return sorted([s for s in out if s["km"] <= max_km and s["start"] <= start.isoformat() and
                   s["end"] >= end.isoformat()], key=lambda s: s["km"])


def station_daily(station_id: str, start: dt.date, end: dt.date, cache: Cache) -> dict:
    key = cache.key("ghcn-daily", station_id, start.isoformat(), end.isoformat())
    hit = cache.get_json(key)
    if hit is None:
        r = session().get(DATA, params={"dataset": "daily-summaries", "stations": station_id,
                                        "startDate": start.isoformat(), "endDate": end.isoformat(),
                                        "dataTypes": "PRCP,TMAX,TMIN", "units": "metric", "format": "json"},
                          timeout=90)
        r.raise_for_status()
        hit = r.json()
        cache.put_json(key, hit)
    out = {}
    for row in hit:
        def f(k):
            v = row.get(k)
            try:
                return None if v in (None, "") else float(v)
            except ValueError:
                return None
        out[row["DATE"][:10]] = {"PRCP": f("PRCP"), "TMAX": f("TMAX"), "TMIN": f("TMIN")}
    return out


def merge_station(records: list[dict], lat: float, lon: float, start: dt.date, end: dt.date, cache: Cache,
                  max_km: float = 15.0, min_cover: float = 0.85):
    """Gauge TMAX/TMIN/RAIN where reported (and physically consistent), gridded elsewhere."""
    try:
        cands = nearest_stations(lat, lon, start, end, cache, max_km)
    except Exception as e:  # noqa: BLE001 -- the gridded weather still stands
        return records, None, {"note": f"station search failed: {e}"}
    days = [r for r in records if start <= r["DAY"] <= end]
    for st in cands:
        try:
            obs = station_daily(st["id"], start, end, cache)
        except Exception:  # noqa: BLE001
            continue
        rain_cover = sum(1 for r in days if (obs.get(r["DAY"].isoformat()) or {}).get("PRCP") is not None) / max(1, len(days))
        if rain_cover < min_cover:
            continue
        n_t = n_p = 0
        out = []
        for r in records:
            o = obs.get(r["DAY"].isoformat()) if start <= r["DAY"] <= end else None
            if o:
                r = dict(r)
                if o["PRCP"] is not None and 0 <= o["PRCP"] < 500:
                    r["RAIN"] = o["PRCP"]
                    n_p += 1
                tx, tn = o["TMAX"], o["TMIN"]
                if tx is not None and tn is not None and -60 < tn <= tx < 60 and tx - tn < 40:
                    r["TMAX"], r["TMIN"] = tx, tn
                    n_t += 1
            out.append(r)
        prov = Provenance(f"GHCN-Daily station {st['id']} {st['name']} ({st['km']} km): rain and temperature",
                          DATA, "public domain (NOAA)", today(), ATTRIB, True,
                          f"{n_p} days of gauge rain, {n_t} of gauge temperature; radiation, humidity, wind gridded")
        return out, prov, {"station": st, "rain_days": n_p, "temp_days": n_t}
    return records, None, {"note": f"no GHCN station within {max_km:.0f} km with >= {min_cover:.0%} of days"}
