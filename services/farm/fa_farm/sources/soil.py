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
# The whole root zone of a deep-rooted crop (maize roots reach ~1.5 m in deep loess / Mollisol
# soils: Ordonez et al. 2018, Field Crops Res. 215): 0-150 cm, the 100-200 cm layer half-weighted.
DEPTHS_ROOTZONE = DEPTHS + (("60-100cm", 40), ("100-200cm", 50))
# property -> scale to SI-ish: sand/clay g/kg -> fraction; soc dg/kg -> %; bdod cg/cm3 -> g/cm3
PROPS = {"sand": 1e-3, "clay": 1e-3, "soc": 1e-2, "bdod": 1e-2}
# WCS: the same 250 m grid, a ~1 kB window per layer (each VRT index above is ~6 MB, which a slow
# link could not fetch within a minute). Homolosine is ISRIC's grid; EPSG:152160 is their code for it.
WCS_URL = "https://maps.isric.org/mapserv"
IGH = "+proj=igh +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +no_defs"
IGH_URN = "http://www.opengis.net/def/crs/EPSG/0/152160"
WCS_HALF_M = 1125.0                       # 9 x 9 cells: the point and every cell within ~1 km


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


def _wcs_window(prop: str, depth: str, x: float, y: float, half: float):
    """(int16 array, (left, top, cell)) for one layer around (x, y) in Homolosine metres; 0 = no data."""
    from rasterio.io import MemoryFile

    from .base import session
    r = session().get(WCS_URL, params={
        "map": f"/map/{prop}.map", "SERVICE": "WCS", "VERSION": "2.0.1", "REQUEST": "GetCoverage",
        "COVERAGEID": f"{prop}_{depth}_mean", "FORMAT": "GEOTIFF_INT16",
        "SUBSET": [f"X({x - half},{x + half})", f"Y({y - half},{y + half})"],
        "SUBSETTINGCRS": IGH_URN, "OUTPUTCRS": IGH_URN}, timeout=30)
    r.raise_for_status()
    with MemoryFile(r.content) as mem, mem.open() as src:
        t = src.transform
        return src.read(1), (t.c, t.f, t.a)


def _profile_wcs(lat: float, lon: float, progress=None, depths=DEPTHS) -> dict:
    """Depth-weighted profile at the nearest cell (within ~1 km) that has a value in every
    layer: 16 small WCS requests, 8 at a time."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from pyproj import Transformer
    to_igh = Transformer.from_crs("EPSG:4326", IGH, always_xy=True)
    x, y = to_igh.transform(lon, lat)
    jobs = [(prop, depth, w) for prop in PROPS for depth, w in depths]
    got: dict = {}
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_wcs_window, p, d, x, y, WCS_HALF_M): (p, d) for p, d, _ in jobs}
            for fut in as_completed(futs):
                got[futs[fut]] = fut.result()
                if progress:
                    progress(len(got), len(jobs))
    except Exception as e:  # noqa: BLE001
        raise DataUnavailable("soil (SoilGrids WCS)", str(e)) from e
    shapes = {a.shape for a, _ in got.values()}
    grids = {g for _, g in got.values()}
    if len(shapes) != 1 or len(grids) != 1:
        raise DataUnavailable("soil (SoilGrids WCS)", "layers came back on different grids")
    (left, top, cell), (rows, cols) = grids.pop(), shapes.pop()
    valid = np.ones((rows, cols), bool)
    for (p, _), (a, _) in got.items():
        if p in ("sand", "bdod"):                 # 0 g/kg sand or 0 bulk density = masked cell
            valid &= a > 0
    cy, cx = np.mgrid[0:rows, 0:cols]
    dist = np.hypot(left + (cx + 0.5) * cell - x, top - (cy + 0.5) * cell - y)
    dist[~valid] = np.inf
    if not np.isfinite(dist.min()) or dist.min() > 1000.0 + cell:
        raise DataUnavailable("soil", f"no SoilGrids value within 1 km of ({lat}, {lon})")
    r, c = np.unravel_index(int(np.argmin(dist)), dist.shape)
    vals = {}
    for prop, scale in PROPS.items():
        pairs = [(float(got[(prop, d)][0][r, c]), w) for d, w in depths]
        vals[prop] = sum(v * scale * w for v, w in pairs) / sum(w for _, w in pairs)
    plon, plat = Transformer.from_crs(IGH, "EPSG:4326", always_xy=True).transform(
        left + (c + 0.5) * cell, top - (r + 0.5) * cell)
    d = float(dist[r, c])
    return {"sand": vals["sand"], "clay": vals["clay"], "soc_pct": vals["soc"], "bdod": vals["bdod"],
            "sampled_at": [plat, plon, 0.0 if d < cell / 2 else round(d, 0)]}


def _search_points(lat: float, lon: float, step_m: float = 250.0, rings: int = 4):
    """The point itself, then rings of 8 points every ``step_m`` out to ~1 km."""
    yield lat, lon, 0.0
    dlat = step_m / 111_320.0
    dlon = step_m / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
    for r in range(1, rings + 1):
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            yield lat + r * dy * dlat, lon + r * dx * dlon, r * step_m * math.hypot(dy, dx)


def _profile_at(lat: float, lon: float) -> dict:
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
    return {"sand": vals["sand"], "clay": vals["clay"], "soc_pct": vals["soc"], "bdod": vals["bdod"]}


def soilgrids_profile(lat: float, lon: float, cache: Cache, rdmsol_cm: float = 120.0, progress=None):
    """Depth-weighted 0-60 cm sand, clay, SOC and bulk density -> (profile, SoilParams, Provenance).
    WCS windows first (~20 kB in all); the VRT point reads (~6 MB per layer) if WCS fails.
    ``progress(done, total)`` counts layers read."""
    key = cache.key("soilgrids", round(lat, 4), round(lon, 4))
    hit = cache.get_json(key)
    if hit is None:
        try:
            hit = _profile_wcs(lat, lon, progress)
        except DataUnavailable as e:
            if "within 1 km" in str(e):
                raise
            hit = None
        tried = []
        for plat, plon, dist in ([] if hit is not None else _search_points(lat, lon)):
            try:
                hit = _profile_at(plat, plon)
                hit["sampled_at"] = [plat, plon, dist]
                break
            except DataUnavailable as e:
                tried.append(str(e))
                if "no SoilGrids value" not in str(e):
                    raise
        if hit is None:
            raise DataUnavailable("soil", f"no SoilGrids value within 1 km of ({lat}, {lon})")
        cache.put_json(key, hit)
    sp = to_soil_params(hit, rdmsol_cm)
    note = "prior at 250 m; not a measurement of this field"
    at = hit.get("sampled_at")
    if at and at[2] > 0:
        note += f"; field centre masked (paddy/water/urban): nearest valid cell {at[2]:.0f} m away"
    prov = Provenance("SoilGrids 2.0 (250 m), depth-weighted 0-60 cm; Saxton & Rawls 2006 PTF",
                      SG_URL, "CC-BY-4.0", today(), SG_ATTRIB, True, note)
    return hit, sp, prov


def soilgrids_rootzone(lat: float, lon: float, cache: Cache, depth_cm: float = 150.0, progress=None):
    """Soil water parameters over the whole root zone (0-150 cm by default) instead of the top
    60 cm: (profile, SoilParams with RDMSOL = depth_cm, Provenance). Deep-rooted crops (maize,
    wheat, sunflower, cotton) draw on the subsoil in dry spells; a 60 cm soil starves them."""
    key = cache.key("soilgrids-rootzone", round(lat, 4), round(lon, 4))
    hit = cache.get_json(key)
    if hit is None:
        hit = _profile_wcs(lat, lon, progress, depths=DEPTHS_ROOTZONE)
        cache.put_json(key, hit)
    sp = to_soil_params(hit, depth_cm)
    prov = Provenance(f"SoilGrids 2.0 (250 m), depth-weighted 0-{depth_cm:.0f} cm; Saxton & Rawls 2006 PTF",
                      SG_URL, "CC-BY-4.0", today(), SG_ATTRIB, True, "prior at 250 m; not a measurement of this field")
    return hit, sp, prov
