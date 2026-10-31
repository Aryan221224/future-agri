"""Observed daily weather anywhere on Earth from NASA POWER (open, commercial use
allowed). Records use the twin's units: RAIN mm/day, IRRAD J/m2/day, VAP hPa,
WIND m/s at 2 m, temperatures in C; RH (%) is kept for disease models.

Open-Meteo's ERA5 archive is a fine alternative for research, but its free tier
is non-commercial; it is therefore not the default for a product.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

from .base import Cache, Provenance, fetch_json, today

POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
POWER_PARAMS = "T2M_MAX,T2M_MIN,T2M,PRECTOTCORR,ALLSKY_SFC_SW_DWN,WS2M,T2MDEW,RH2M"
POWER_ATTRIB = ("These data were obtained from the NASA Langley Research Center (LaRC) POWER "
                "Project funded through the NASA Earth Science/Applied Science Program.")


def es_hpa(t_c: float) -> float:
    """Saturation vapour pressure (Tetens), hPa."""
    return 6.108 * math.exp(17.27 * t_c / (t_c + 237.3))


def parse_power(raw: dict) -> tuple[list[dict], float]:
    """POWER daily JSON -> (records, elevation_m). Days with any missing value are
    linearly filled when the gap is <= 3 days; longer gaps raise."""
    fill = float(raw.get("header", {}).get("fill_value", -999.0))
    par = raw["properties"]["parameter"]
    coords = raw.get("geometry", {}).get("coordinates", [None, None, 0.0])
    elev = float(coords[2]) if len(coords) > 2 and coords[2] is not None else 0.0
    keys = sorted(par["T2M_MAX"].keys())
    rows = []
    for k in keys:
        def g(name):
            v = par[name].get(k, fill)
            return None if v is None or float(v) == fill else float(v)
        rows.append({
            "DAY": dt.datetime.strptime(k, "%Y%m%d").date(),
            "TMAX": g("T2M_MAX"), "TMIN": g("T2M_MIN"), "RAIN": g("PRECTOTCORR"),
            "IRRAD": None if g("ALLSKY_SFC_SW_DWN") is None else g("ALLSKY_SFC_SW_DWN") * 1e6,
            "VAP": None if g("T2MDEW") is None else es_hpa(g("T2MDEW")),
            "WIND": g("WS2M"), "RH": g("RH2M"),
        })
    _fill_gaps(rows)
    for r in rows:
        if r["TMIN"] > r["TMAX"]:
            r["TMIN"], r["TMAX"] = r["TMAX"], r["TMIN"]
        r["RAIN"] = max(0.0, r["RAIN"])
    return rows, elev


def _fill_gaps(rows: list[dict], max_gap: int = 3) -> None:
    fields = [f for f in ("TMAX", "TMIN", "RAIN", "IRRAD", "VAP", "WIND", "RH")]
    for f in fields:
        n = len(rows)
        i = 0
        while i < n:
            if rows[i][f] is not None:
                i += 1
                continue
            j = i
            while j < n and rows[j][f] is None:
                j += 1
            gap = j - i
            if gap > max_gap or i == 0 or j == n:
                if f == "RH":           # RH is auxiliary; derive from VAP/T if missing
                    for k in range(i, j):
                        t = 0.5 * (rows[k]["TMAX"] + rows[k]["TMIN"]) if rows[k]["TMAX"] is not None else 15.0
                        v = rows[k]["VAP"] or es_hpa(t) * 0.7
                        rows[k][f] = min(100.0, 100.0 * v / es_hpa(t))
                    i = j
                    continue
                raise ValueError(f"NASA POWER: {gap} missing days of {f} from {rows[i]['DAY']}")
            a, b = rows[i - 1][f], rows[j][f]
            for k in range(i, j):
                w = (k - i + 1) / (gap + 1)
                rows[k][f] = a + w * (b - a)
            i = j


def power_daily(lat: float, lon: float, start: dt.date, end: dt.date,
                cache: Cache) -> tuple[list[dict], Provenance, float]:
    """Observed daily records for [start, end] from NASA POWER (AG community)."""
    params = dict(parameters=POWER_PARAMS, community="AG", longitude=round(lon, 4),
                  latitude=round(lat, 4), start=start.strftime("%Y%m%d"),
                  end=end.strftime("%Y%m%d"), format="JSON")
    raw = fetch_json(POWER_URL, params, cache, layer="weather history", timeout=120)
    recs, elev = parse_power(raw)
    recs = [r for r in recs if start <= r["DAY"] <= end]
    prov = Provenance("NASA POWER daily (AG, MERRA-2/CERES based)", POWER_URL,
                      "NASA open data (no restrictions)", today(), POWER_ATTRIB, True)
    return recs, prov, elev


def monthly_climatology(records: list[dict]) -> dict[int, dict]:
    acc: dict[int, dict] = defaultdict(lambda: defaultdict(list))
    for r in records:
        for k in ("TMAX", "TMIN", "RAIN", "IRRAD", "VAP", "WIND"):
            acc[r["DAY"].month][k].append(r[k])
    return {m: {k: sum(v) / len(v) for k, v in d.items()} for m, d in acc.items()}
