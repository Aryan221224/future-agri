"""Soil water at sowing from the weather since the last harvest, instead of a fixed guess.

A daily bucket for the potential root zone, run from 1 October of the previous year (soil
assumed at half its available water after the previous crop) to sowing: rain in; bare-soil
evaporation by Ritchie's two-stage model (stage 1 at the full energy-limited rate for the
first ~6 mm after wetting, then falling with the square root of days since wetting:
Ritchie 1972, Water Resour. Res. 8); winter evaporation small because ET0 is; water above
field capacity drains. Frozen ground and snowmelt are approximated by letting precipitation
on days below 0 C wait as snow and melt on the first thaw days.

    available_water_at_sowing(records, soil, sowing, root_depth_cm) -> WAV (cm)
"""
from __future__ import annotations

import datetime as dt
import math


def available_water_at_sowing(records: list[dict], smfcf: float, smw: float, sowing: dt.date,
                              depth_cm: float = 150.0, start_frac: float = 0.5,
                              stage1_mm: float = 6.0, alpha_mm: float = 3.5, lat: float = 45.0,
                              elev: float = 200.0, evap_factor: float = 1.0) -> dict:
    """Plant-available water (cm) in the root zone at sowing. Soil evaporation demand is PCSE's
    Penman-Monteith ES0 for wet bare soil, from the same weather the crop model reads, times
    ``evap_factor`` for residue left on the surface (``fa_twin.management``)."""
    from pcse.util import reference_ET
    cap = max(0.0, (smfcf - smw) * depth_cm * 10.0)      # mm of plant-available water
    start = dt.date(sowing.year - 1, 10, 1)
    days = sorted((r for r in records if start <= r["DAY"] < sowing), key=lambda r: r["DAY"])
    if len(days) < 0.8 * (sowing - start).days:
        raise ValueError("not enough weather records since 1 October to estimate soil water")
    w, snow, cum_e1, t_dry, drained = start_frac * cap, 0.0, 0.0, 0, 0.0
    for r in days:
        tmean = (r["TMIN"] + r["TMAX"]) / 2
        p = float(r["RAIN"])
        if tmean < 0.0:
            snow += p
            p = 0.0
        elif snow > 0:
            melt = min(snow, 4.0 * tmean)                 # degree-day melt, 4 mm / C / day
            snow -= melt
            p += melt
        if p > 2.0:
            cum_e1, t_dry = 0.0, 0
        _, es0, _ = reference_ET(DAY=r["DAY"], LAT=lat, ELEV=elev, TMIN=r["TMIN"], TMAX=r["TMAX"],
                                 IRRAD=r["IRRAD"], VAP=r["VAP"], WIND=r["WIND"], ANGSTA=0.25, ANGSTB=0.5,
                                 ETMODEL="PM")
        et0 = float(es0) * evap_factor                    # mm/day
        if tmean < 0.0 or snow > 5.0:
            e = 0.0
        elif cum_e1 < stage1_mm:
            e = min(et0, stage1_mm - cum_e1 + 0.0)
            cum_e1 += e
        else:
            t_dry += 1
            e = min(et0, alpha_mm * (math.sqrt(t_dry) - math.sqrt(t_dry - 1)))
        w = w + p - e
        if w > cap:
            drained += w - cap
            w = cap
        w = max(0.0, w)
    return {"WAV_cm": round(w / 10.0, 2), "capacity_cm": round(cap / 10.0, 2), "fraction": round(w / cap, 3) if cap else 0.0,
            "drained_cm": round(drained / 10.0, 1), "days": len(days)}
