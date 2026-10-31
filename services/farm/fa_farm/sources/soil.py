"""Soil for any location: SoilGrids 2.0 (250 m, CC-BY-4.0) -> Saxton & Rawls (2006)
pedotransfer -> WOFOST water-balance parameters.

SoilGrids is a *prior*: a 250 m prediction with wide uncertainty. Within-field
variation is added on top by the ground-truth generator, and the twin never
sees these values directly.
"""
from __future__ import annotations

import math

import numpy as np

from .base import Cache, DataUnavailable, Provenance, gdal_env, today

SG_URL = "https://files.isric.org/soilgrids/latest/data"
SG_ATTRIB = ("SoilGrids 2.0, ISRIC - World Soil Information (Poggio et al. 2021, SOIL 7:217-240), "
             "CC-BY-4.0")
DEPTHS = (("0-5cm", 5), ("5-15cm", 10), ("15-30cm", 15), ("30-60cm", 30))
# property -> scale to SI-ish: sand/clay g/kg -> fraction; soc dg/kg -> %; bdod cg/cm3 -> g/cm3
PROPS = {"sand": 1e-3, "clay": 1e-3, "soc": 1e-2, "bdod": 1e-2}


def saxton_rawls(sand: float, clay: float, om_pct: float) -> dict:
    """Saxton & Rawls (2006, SSSAJ 70:1569) moisture equations, Table 1.
    sand, clay: fractions (0-1); om_pct: organic matter, % by weight."""
    S, C, OM = sand, clay, min(om_pct, 8.0)
    t1500t = (-0.024 * S + 0.487 * C + 0.006 * OM + 0.005 * S * OM - 0.013 * C * OM
              + 0.068 * S * C + 0.031)
    t1500 = t1500t + (0.14 * t1500t - 0.02)
    t33t = (-0.251 * S + 0.195 * C + 0.011 * OM + 0.006 * S * OM - 0.027 * C * OM
            + 0.452 * S * C + 0.299)
    t33 = t33t + (1.283 * t33t ** 2 - 0.374 * t33t - 0.015)
    ts33t = (0.278 * S + 0.034 * C + 0.022 * OM - 0.018 * S * OM - 0.027 * C * OM
             - 0.584 * S * C + 0.078)
    ts33 = ts33t + (0.636 * ts33t - 0.107)
    ts = t33 + ts33 - 0.097 * S + 0.043
    t1500 = max(0.01, t1500)
    t33 = max(t1500 + 0.02, t33)
    ts = max(t33 + 0.03, ts)
    B = (math.log(1500) - math.log(33)) / (math.log(t33) - math.log(t1500))
    lam = 1.0 / B
    ksat = 1930.0 * (ts - t33) ** (3.0 - lam)
    return {"SMW": t1500, "SMFCF": t33, "SM0": ts, "KSAT_mm_h": ksat, "lambda": lam}


def to_soil_params(profile: dict, rdmsol_cm: float = 120.0):
    """Profile (sand, clay fractions; soc_pct) -> fa_twin SoilParams."""
    from fa_twin.engine import SoilParams
    om = profile["soc_pct"] * 1.724            # van Bemmelen factor
    sr = saxton_rawls(profile["sand"], profile["clay"], om)
    k0 = sr["KSAT_mm_h"] * 24.0 / 10.0        # cm/day
    return SoilParams(SMFCF=round(sr["SMFCF"], 4), SM0=round(sr["SM0"], 4),
                      SMW=round(sr["SMW"], 4), CRAIRC=0.06, RDMSOL=float(rdmsol_cm),
                      K0=round(k0, 3), SOPE=round(float(np.clip(k0, 0.5, 10.0)), 3),
                      KSUB=round(float(np.clip(0.5 * k0, 0.2, 5.0)), 3))


def _sample_vrt(url: str, lat: float, lon: float) -> float:
    import rasterio
    from pyproj import Transformer
    with rasterio.Env(**gdal_env()):
        with rasterio.open("/vsicurl/" + url) as src:
            tr = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            x, y = tr.transform(lon, lat)
            v = float(list(src.sample([(x, y)]))[0][0])
            nod = src.nodata
    if nod is not None and v == nod:
        raise DataUnavailable("soil", f"no SoilGrids value at ({lat}, {lon}) -- water or urban?")
    return v


def soilgrids_profile(lat: float, lon: float, cache: Cache, rdmsol_cm: float = 120.0):
    """Depth-weighted 0-60 cm sand, clay, SOC and bulk density -> (profile, SoilParams, Provenance)."""
    key = cache.key("soilgrids", round(lat, 4), round(lon, 4))
    hit = cache.get_json(key)
    if hit is None:
        from concurrent.futures import ThreadPoolExecutor
        jobs = [(prop, depth, w) for prop in PROPS for depth, w in DEPTHS]
        try:
            with ThreadPoolExecutor(max_workers=8) as ex:
                got = list(ex.map(lambda j: _sample_vrt(
                    f"{SG_URL}/{j[0]}/{j[0]}_{j[1]}_mean.vrt", lat, lon), jobs))
        except DataUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise DataUnavailable("soil", str(e)) from e
        vals = {}
        for prop, scale in PROPS.items():
            pairs = [(v, w) for (p, _, w), v in zip(jobs, got) if p == prop]
            vals[prop] = sum(v * scale * w for v, w in pairs) / sum(w for _, w in pairs)
        hit = {"sand": vals["sand"], "clay": vals["clay"], "soc_pct": vals["soc"],
               "bdod": vals["bdod"]}
        cache.put_json(key, hit)
    sp = to_soil_params(hit, rdmsol_cm)
    prov = Provenance("SoilGrids 2.0 (250 m), depth-weighted 0-60 cm; Saxton & Rawls 2006 PTF",
                      SG_URL, "CC-BY-4.0", today(), SG_ATTRIB, True,
                      "prior at 250 m; not a measurement of this field")
    return hit, sp, prov
