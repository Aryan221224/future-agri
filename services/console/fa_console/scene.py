"""3D scene payload for a farm, in a local metric frame (the farm grid's UTM
coordinates). Terrain is the Copernicus DEM sampled bilinearly on a regular
vertex grid around the field; the ground texture is the clearest Sentinel-2
true-colour scene reprojected onto the same frame. Per-cell arrays are base64
little-endian (float32 positions, float16 layers)."""
from __future__ import annotations

import base64
import datetime as dt
import math

import numpy as np

from fa_farm.archetypes import season_dates
from fa_farm.grid import FarmGrid
from fa_farm.sources.base import DataUnavailable
from fa_farm.sources.terrain import dem_window

MARGIN_M = 30.0


def b64(arr, dtype: str = "<f4") -> str:
    return base64.b64encode(np.ascontiguousarray(np.asarray(arr, dtype=dtype)).tobytes()).decode()


def farm_grid(vf) -> FarmGrid:
    return FarmGrid.from_wgs84(vf.spec.polygon_wgs84, vf.spec.cell_m)


def _half(grid: FarmGrid) -> float:
    minx, miny, maxx, maxy = grid.bounds_local
    return max(maxx - minx, maxy - miny) / 2 + 150.0


def frame(grid: FarmGrid, max_vertices: int = 129):
    """Regular vertex grid covering the field plus a margin -> (xs, ys, step)."""
    minx, miny, maxx, maxy = grid.bounds_local
    x0, y0, x1, y1 = minx - MARGIN_M, miny - MARGIN_M, maxx + MARGIN_M, maxy + MARGIN_M
    step = max(max(x1 - x0, y1 - y0) / (max_vertices - 1), 1.0)
    nx = int(math.ceil((x1 - x0) / step)) + 1
    ny = int(math.ceil((y1 - y0) / step)) + 1
    return x0 + np.arange(nx) * step, y0 + np.arange(ny) * step, step


def _lonlat(grid: FarmGrid, x: np.ndarray, y: np.ndarray):
    from pyproj import Transformer
    tr = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    return tr.transform(np.asarray(x, float), np.asarray(y, float))


def _bilinear(dem: np.ndarray, lat0: float, lon0: float, half: float, lon, lat) -> np.ndarray:
    """DEM window centred on (lat0, lon0) with half-size ``half`` m, rows north->south."""
    dlat = half / 111_320.0
    dlon = half / (111_320.0 * max(0.1, math.cos(math.radians(lat0))))
    ny, nx = dem.shape
    r = (lat0 + dlat - np.asarray(lat)) / (2 * dlat) * ny - 0.5
    c = (np.asarray(lon) - (lon0 - dlon)) / (2 * dlon) * nx - 0.5
    r = np.clip(r, 0, ny - 1.0001)
    c = np.clip(c, 0, nx - 1.0001)
    r0, c0 = np.floor(r).astype(int), np.floor(c).astype(int)
    fr, fc = r - r0, c - c0
    d = np.where(np.isfinite(dem), dem, np.nanmean(dem) if np.isfinite(dem).any() else 0.0)
    return ((1 - fr) * (1 - fc) * d[r0, c0] + (1 - fr) * fc * d[r0, c0 + 1]
            + fr * (1 - fc) * d[r0 + 1, c0] + fr * fc * d[r0 + 1, c0 + 1])


def _dem(vf, grid: FarmGrid, cache, offline: bool):
    lat, lon = vf.spec.centroid
    half = _half(grid)
    key = cache.key("dem", round(lat, 5), round(lon, 5), half)
    if offline and cache.get_bytes(key + ".npy") is None:
        raise DataUnavailable("terrain", "offline and not cached")
    dem, res_m, prov = dem_window(lat, lon, half, cache)
    return dem, half


def world_zones(vf, session_seed: int = 0) -> np.ndarray | None:
    """The virtual farm's soil zones exactly as the session's ground truth draws them."""
    from fa_farm.heterogeneity import soil_zones
    spec = vf.spec
    grid = farm_grid(vf)
    n = grid.n_cells
    twi = spec.twi_cells if (spec.twi_cells is not None and len(spec.twi_cells) == n) else None
    rng = np.random.default_rng(spec.seed * 1000 + session_seed)
    k = max(1, min(spec.n_zones, n // 4 if n >= 4 else 1))
    return soil_zones(grid, spec.soil, twi, rng, k=k)[0]


def build_scene(vf, cache, offline: bool = False, mode: str = "sim", session_seed: int = 0,
                buffer_m: float = 0.0) -> dict:
    grid = farm_grid(vf)
    warnings: list[str] = []
    xs, ys, step = frame(grid)
    X, Y = np.meshgrid(xs, ys)
    heights = np.zeros(X.shape)
    abs_min = 0.0
    try:
        dem, half = _dem(vf, grid, cache, offline)
        lon, lat = _lonlat(grid, X.ravel(), Y.ravel())
        lat0, lon0 = vf.spec.centroid
        h = _bilinear(dem, lat0, lon0, half, lon, lat).reshape(X.shape)
        if np.isfinite(dem).any():
            abs_min = float(np.nanmin(h))
            heights = h - abs_min
        else:
            warnings.append("terrain has no elevation data here; shown flat")
    except DataUnavailable as e:
        warnings.append(f"terrain not available ({'offline and not cached' if offline else e}); shown flat")
    xy = grid.cells_xy
    n = grid.n_cells
    layers, layer_meta = {}, {}
    twi = vf.spec.twi_cells if (vf.spec.twi_cells is not None and len(vf.spec.twi_cells) == n) else None
    if twi is not None:
        layers["twi_b64"] = b64(twi, "<f2")
        layer_meta["twi"] = "terrain wetness index (Copernicus DEM)"
    if mode == "sim":
        zone = world_zones(vf, session_seed)
        layer_meta["zone"] = "soil zones of the virtual farm's hidden ground truth"
    else:
        zone = (np.digitize(twi, np.quantile(twi, [0.25, 0.5, 0.75])) if twi is not None else np.zeros(n))
        layer_meta["zone"] = "terrain zones by wetness quartile (the twin's zones come from the scan)"
    layers["zone_b64"] = b64(zone, "<f2")
    ring = list(grid.polygon_local.exterior.coords)
    inner = None
    if buffer_m > 0:
        b = grid.polygon_local.buffer(-buffer_m)
        if not b.is_empty and b.geom_type == "Polygon":
            inner = [[round(x, 2), round(y, 2)] for x, y in b.exterior.coords]
    lat, lon = vf.spec.centroid
    a = vf.spec.archetype
    sow, harv = season_dates(a, vf.spec.year)
    return {
        "origin": {"lat": round(lat, 7), "lon": round(lon, 7), "crs": grid.crs.to_string()
                   if hasattr(grid.crs, "to_string") else str(grid.crs)},
        "polygon": [[round(x, 2), round(y, 2)] for x, y in ring],
        "buffer": {"m": buffer_m, "inner": inner},
        "bounds": [round(v, 2) for v in grid.bounds_local],
        "terrain": {"nx": len(xs), "ny": len(ys), "x0": round(float(xs[0]), 2), "y0": round(float(ys[0]), 2),
                    "dx": round(step, 3), "dy": round(step, 3), "heights_b64": b64(heights.ravel(), "<f4"),
                    "min": round(abs_min, 2), "max": round(abs_min + float(heights.max()), 2)},
        "cells": {"n": n, "cell_m": grid.cell_m, "x_b64": b64(xy[:, 0]), "y_b64": b64(xy[:, 1]),
                  "ground_b64": b64(_bilinear_on_frame(heights, xs, ys, xy), "<f4")},
        "layers": layers, "layer_meta": layer_meta,
        "crop": {"name": a.crop, "variety": a.variety, "sowing": sow.isoformat(), "harvest": harv.isoformat()},
        "imagery": {"available": False},
        "area_ha": round(grid.area_ha, 3), "warnings": warnings}


def _bilinear_on_frame(heights: np.ndarray, xs: np.ndarray, ys: np.ndarray, xy: np.ndarray) -> np.ndarray:
    """Ground height under each cell centre from the terrain vertex grid."""
    fx = np.clip((xy[:, 0] - xs[0]) / (xs[1] - xs[0]) if len(xs) > 1 else 0 * xy[:, 0], 0, len(xs) - 1.0001)
    fy = np.clip((xy[:, 1] - ys[0]) / (ys[1] - ys[0]) if len(ys) > 1 else 0 * xy[:, 1], 0, len(ys) - 1.0001)
    c0, r0 = np.floor(fx).astype(int), np.floor(fy).astype(int)
    if len(xs) < 2 or len(ys) < 2:
        return np.zeros(len(xy))
    ax, ay = fx - c0, fy - r0
    h = heights
    return ((1 - ax) * (1 - ay) * h[r0, c0] + ax * (1 - ay) * h[r0, c0 + 1]
            + (1 - ax) * ay * h[r0 + 1, c0] + ax * ay * h[r0 + 1, c0 + 1])


def imagery_png(vf, cache, offline: bool = False, mode: str = "sim", today: dt.date | None = None,
                px_m: float = 2.5, max_px: int = 1024):
    """Sentinel-2 true colour reprojected onto the scene frame -> (png bytes, meta)."""
    from fa_farm.sources.satellite import encode_png, s2_rgb_chip
    a = vf.spec.archetype
    sow, harv = season_dates(a, vf.spec.year)
    if mode == "sim":
        start, end = sow + dt.timedelta(days=40), harv - dt.timedelta(days=10)
    else:
        end = today or dt.date.today()
        start = end - dt.timedelta(days=60)
    poly = vf.spec.polygon_wgs84
    key = cache.key("s2rgb", poly.wkt, start.isoformat(), end.isoformat(), 60.0, 40.0)
    if offline and cache.get_bytes(key + ".npy") is None:
        raise DataUnavailable("satellite", "offline and not cached")
    rgb, meta, prov = s2_rgb_chip(poly, start, end, cache)
    grid = farm_grid(vf)
    xs, ys, _ = frame(grid)
    x0, x1, y0, y1 = xs[0], xs[-1], ys[0], ys[-1]
    w = int(min(max_px, max(2, math.ceil((x1 - x0) / px_m))))
    h = int(min(max_px, max(2, math.ceil((y1 - y0) / px_m))))
    px = x0 + (np.arange(w) + 0.5) * (x1 - x0) / w
    py = y1 - (np.arange(h) + 0.5) * (y1 - y0) / h               # image rows north -> south
    PX, PY = np.meshgrid(px, py)
    lon, lat = _lonlat(grid, PX.ravel(), PY.ravel())
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", meta["crs"], always_xy=True)
    ix, iy = tr.transform(lon, lat)
    a_, b_, c_, d_, e_, f_ = meta["transform"]
    col = np.floor((np.asarray(ix) - c_) / a_).astype(int)
    row = np.floor((np.asarray(iy) - f_) / e_).astype(int)
    H, W = rgb.shape[:2]
    ok = (col >= 0) & (col < W) & (row >= 0) & (row < H)
    out = np.zeros((h * w, 3), np.uint8)
    out[ok] = rgb[row[ok], col[ok]]
    img = out.reshape(h, w, 3)
    return encode_png(img), {"date": meta["date"], "cloud": meta["cloud"], "scene": meta["scene"],
                             "bounds": [round(float(x0), 2), round(float(y0), 2), round(float(x1), 2),
                                        round(float(y1), 2)], "provenance": prov.as_dict()}
