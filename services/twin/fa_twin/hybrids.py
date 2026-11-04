"""Crop phenology from what the seed bag says, not from a European variety table.

US and many other seed companies rate maize hybrids by growing degree units to black layer
(GDU, deg F, the 86/50 method: daily max capped at 86 F, min floored at 50 F, base 50 F) and a
relative maturity (CRM, days). WOFOST counts its own effective temperature (DTSMTB of the daily
mean). The two clocks run at different rates, so the rating is converted on the site's own
*historical* weather (never the season being simulated): emergence at ~100 GDU (Nielsen, Purdue
Corn & Soybean Field Guide), silking at about half of black-layer GDU (Abendroth et al. 2011,
"Corn Growth and Development", Iowa State PMR 1009), black layer at the rating.

    tsums_for_hybrid(records, sowing_mmdd, gdu_black_layer, crop_params) -> {"TSUM1", "TSUM2", ...}
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

EMERGENCE_GDU_F = 100.0
SILKING_SHARE = 0.52                 # silking GDU / black-layer GDU (ISU PMR 1009 stage table)

# GDU to black layer for a relative maturity, from the hybrids planted at Mead (AmeriFlux US-Ne1/2/3
# planting records: Pioneer P1197 111 CRM 2730 GDU; Dekalb 65-79 115 CRM 2875 GDU) -- ~36 GDU/day
CRM_POINTS = ((111.0, 2730.0), (115.0, 2875.0))


def gdu_for_crm(crm: float) -> float:
    (c0, g0), (c1, g1) = CRM_POINTS
    return g0 + (crm - c0) * (g1 - g0) / (c1 - c0)


def gdu_f(tmin_c: float, tmax_c: float) -> float:
    """Daily corn GDU (deg F, 86/50 method)."""
    hi = min(max(tmax_c * 9 / 5 + 32, 50.0), 86.0)
    lo = min(max(tmin_c * 9 / 5 + 32, 50.0), 86.0)
    return (hi + lo) / 2 - 50.0


def _afgen(table: list[float], x: float) -> float:
    xs, ys = table[0::2], table[1::2]
    return float(np.interp(x, xs, ys))


@dataclass
class HybridPhenology:
    TSUMEM: float
    TSUM1: float
    TSUM2: float
    seasons: int
    gdu_black_layer: float

    def overrides(self) -> dict:
        return {"TSUMEM": round(self.TSUMEM, 1), "TSUM1": round(self.TSUM1, 1), "TSUM2": round(self.TSUM2, 1)}


def tsums_for_hybrid(records: list[dict], sowing_mmdd: str, gdu_black_layer: float, crop: dict,
                     years: list[int] | None = None) -> HybridPhenology:
    """WOFOST TSUMEM / TSUM1 / TSUM2 for a hybrid rated ``gdu_black_layer`` (deg F), averaged over
    the historical seasons in ``records`` (daily dicts with DAY, TMIN, TMAX in deg C).
    ``crop`` gives DTSMTB, TBASEM, TEFFMX (the WOFOST variety being re-timed)."""
    by_day = {r["DAY"]: r for r in records}
    sm, sd = (int(x) for x in sowing_mmdd.split("-"))
    yrs = years or sorted({d.year for d in by_day})
    em, t1, t2 = [], [], []
    for y in yrs:
        d = dt.date(y, sm, sd)
        g = te = ts1 = ts2 = 0.0
        stage = 0                      # 0 sowing->emergence, 1 ->silking, 2 ->black layer
        ok = False
        for _ in range(240):
            r = by_day.get(d)
            if r is None:
                break
            tmean = (r["TMIN"] + r["TMAX"]) / 2
            g += gdu_f(r["TMIN"], r["TMAX"])
            if stage == 0:
                te += max(0.0, min(tmean, crop["TEFFMX"]) - crop["TBASEM"])
                if g >= EMERGENCE_GDU_F:
                    stage, g_em = 1, g
            elif stage == 1:
                ts1 += _afgen(crop["DTSMTB"], tmean)
                if g >= SILKING_SHARE * gdu_black_layer:
                    stage = 2
            else:
                ts2 += _afgen(crop["DTSMTB"], tmean)
                if g >= gdu_black_layer:
                    ok = True
                    break
            d += dt.timedelta(days=1)
        if ok:
            em.append(te)
            t1.append(ts1)
            t2.append(ts2)
    if not t1:
        raise ValueError(f"no historical season in the records reaches {gdu_black_layer:.0f} GDU")
    return HybridPhenology(float(np.mean(em)), float(np.mean(t1)), float(np.mean(t2)), len(t1), gdu_black_layer)


def crop_table(crop: str, variety: str, engine: str = "Wofost72") -> dict:
    """The variety's parameters (DTSMTB, TBASEM, TEFFMX, RDMCR, ...), regional overrides included."""
    from .engine import crop_provider, resolve_variety
    base, regional, _ = resolve_variety(crop, variety)
    cp = crop_provider(engine)
    cp.set_active_crop(crop, base)
    return {**dict(cp), **regional}


# Maximum rooting depth in deep soils (cm). WOFOST's shipped maize RDMCR is 100 cm; maize roots
# reach ~150 cm in deep Midwest soils (Ordonez et al. 2018; Fan et al. 2016, Field Crops Res. 189).
DEEP_ROOTING_CM = {"maize": 150.0, "wheat": 150.0, "sunflower": 150.0, "cotton": 150.0, "sorghum": 150.0}


def rooting_override(crop: str, rdmsol_cm: float, shipped_rdmcr: float) -> dict:
    deep = DEEP_ROOTING_CM.get(crop)
    if deep is None or rdmsol_cm < 140.0:
        return {}
    return {"RDMCR": float(min(deep, rdmsol_cm, max(shipped_rdmcr, deep)))}
