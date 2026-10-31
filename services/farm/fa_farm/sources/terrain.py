"""Terrain from the Copernicus GLO-30 DEM: elevation, slope and topographic
wetness index (TWI). TWI drives where water collects, which is where crops grow
differently and where the ground-truth generator puts its wetter soil zones.
"""
from __future__ import annotations

import math

import numpy as np

from .base import Cache, DataUnavailable, Provenance, gdal_env, today

DEM_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
DEM_ATTRIB = ("Copernicus DEM GLO-30 (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space "
              "GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA")


def tile_name(lat: float, lon: float) -> str:
    la, lo = math.floor(lat), math.floor(lon)
    ns = "N" if la >= 0 else "S"
    ew = "E" if lo >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(la):02d}_00_{ew}{abs(lo):03d}_00_DEM"


def dem_window(lat: float, lon: float, half_size_m: float, cache: Cache):
    """Elevation window centred on (lat, lon) -> (dem m, res_m, Provenance).
    Cells are ~30 m; the window is returned on its native lat/lon grid with the
    metric resolution of its rows."""
    key = cache.key("dem", round(lat, 5), round(lon, 5), half_size_m)
    blob = cache.get_bytes(key + ".npy")
    meta = cache.get_json(key)
    if blob is None or meta is None:
        import io
        import rasterio
        from rasterio.windows import from_bounds
        name = tile_name(lat, lon)
        url = f"/vsicurl/{DEM_URL}/{name}/{name}.tif"
        dlat = half_size_m / 111_320.0
        dlon = half_size_m / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
        try:
            with rasterio.Env(**gdal_env()):
                with rasterio.open(url) as src:
                    win = from_bounds(lon - dlon, lat - dlat, lon + dlon, lat + dlat, src.transform)
                    arr = src.read(1, window=win, boundless=True, fill_value=np.nan).astype("float32")
                    res_deg = abs(src.transform.a)
        except Exception as e:  # noqa: BLE001 -- ocean tiles do not exist, etc.
            raise DataUnavailable("terrain", f"{name}: {e}") from e
        res_m = res_deg * 111_320.0
        buf = io.BytesIO()
        np.save(buf, arr)
        cache.put_bytes(key + ".npy", buf.getvalue())
        cache.put_json(key, {"res_m": res_m})
        meta = {"res_m": res_m}
        dem = arr
    else:
        import io
        dem = np.load(io.BytesIO(blob))
    prov = Provenance("Copernicus DEM GLO-30", DEM_URL, "Copernicus DEM licence (free, attribution)",
                      today(), DEM_ATTRIB, True)
    return dem, float(meta["res_m"]), prov


def slope_deg(dem: np.ndarray, res_m: float) -> np.ndarray:
    gy, gx = np.gradient(np.nan_to_num(dem, nan=float(np.nanmean(dem))), res_m)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def flow_accumulation(dem: np.ndarray) -> np.ndarray:
    """D8 flow accumulation (cells draining through each cell, incl. itself)."""
    z = np.nan_to_num(dem, nan=float(np.nanmax(dem)) + 1)
    h, w = z.shape
    acc = np.ones_like(z, dtype=float)
    order = np.argsort(-z, axis=None)
    nb = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    for idx in order:
        i, j = divmod(int(idx), w)
        best, bi, bj = 0.0, -1, -1
        for di, dj in nb:
            a, b = i + di, j + dj
            if 0 <= a < h and 0 <= b < w:
                drop = (z[i, j] - z[a, b]) / (math.sqrt(2) if di and dj else 1.0)
                if drop > best:
                    best, bi, bj = drop, a, b
        if bi >= 0:
            acc[bi, bj] += acc[i, j]
    return acc


def twi(dem: np.ndarray, res_m: float) -> np.ndarray:
    """TWI = ln(a / tan(beta)), a = specific catchment area (m)."""
    a = flow_accumulation(dem) * res_m
    tanb = np.tan(np.radians(np.maximum(slope_deg(dem, res_m), 0.1)))
    return np.log(a / tanb)
