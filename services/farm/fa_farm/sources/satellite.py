"""Sentinel-2 L2A cover history of a real field (Earth Search STAC, open data).

The ground-truth farm is anchored to what the real field actually did that
season: field-mean NDVI from cloud-free pixels (SCL mask) converted to
fractional vegetation cover. The twin never receives this series -- it is part
of the truth, not an observation.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from .base import Cache, DataUnavailable, Provenance, gdal_env, today

STAC = "https://earth-search.aws.element84.com/v1"
S2_ATTRIB = "Contains modified Copernicus Sentinel data (ESA), via Element 84 Earth Search"
NDVI_SOIL, NDVI_VEG = 0.15, 0.90
GOOD_SCL = {4, 5}               # vegetation, bare soil (excludes cloud, shadow, water, snow)


def fvc(ndvi: float) -> float:
    """Fractional vegetation cover, Carlson & Ripley (1997) scaled-NDVI squared."""
    x = (ndvi - NDVI_SOIL) / (NDVI_VEG - NDVI_SOIL)
    return float(np.clip(x, 0.0, 1.0) ** 2)


def _offset(item) -> float:
    """Processing baseline >= 04.00 (from 2022-01-25) adds +1000 to L2A DNs.
    Earth Search records whether it already removed it."""
    p = item.properties
    if p.get("earthsearch:boa_offset_applied"):
        return 0.0
    base = str(p.get("s2:processing_baseline", "0"))
    try:
        return -1000.0 if float(base) >= 4.0 else 0.0
    except ValueError:
        return 0.0


def scl_on_grid(scl_ds, red_ds, win):
    """The 20 m scene classification resampled (nearest) onto the 10 m red window:
    same bounds, same shape, whatever the rounding of the windows."""
    from rasterio.enums import Resampling
    bounds = red_ds.window_bounds(win)
    sw = scl_ds.window(*bounds)
    return scl_ds.read(1, window=sw, out_shape=(int(win.height), int(win.width)),
                       resampling=Resampling.nearest, boundless=True, fill_value=0)


def _field_ndvi(item, polygon_wgs84) -> float | None:
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom
    from rasterio.windows import from_bounds
    with rasterio.Env(**gdal_env()):
        with rasterio.open(item.assets["red"].href) as red:
            g = transform_geom("EPSG:4326", red.crs, polygon_wgs84.__geo_interface__)
            from shapely.geometry import shape
            b = shape(g).bounds
            win = from_bounds(*b, red.transform).round_offsets().round_lengths()
            r = red.read(1, window=win).astype(float)
            tr = red.window_transform(win)
            with rasterio.open(item.assets["nir"].href) as nir:     # same 10 m grid as red
                n = nir.read(1, window=win).astype(float)
            with rasterio.open(item.assets["scl"].href) as scl:
                s_up = scl_on_grid(scl, red, win)
    if r.size == 0 or n.shape != r.shape or s_up.shape != r.shape:
        return None
    inside = ~geometry_mask([g], out_shape=r.shape, transform=tr)
    off = _offset(item)
    r, n = (r + off) / 1e4, (n + off) / 1e4
    good = inside & np.isin(s_up, list(GOOD_SCL)) & (r > 0) & (n > 0)
    if good.sum() < max(4, 0.6 * inside.sum()):
        return None                               # too cloudy over the field
    nd = (n[good] - r[good]) / (n[good] + r[good])
    return float(np.median(nd))


def s2_cover_series(polygon_wgs84, start: dt.date, end: dt.date, cache: Cache,
                    max_cloud: float = 60.0) -> tuple[list[tuple[dt.date, float, float]], Provenance]:
    """[(date, field NDVI, FVC)] for cloud-free acquisitions in [start, end]."""
    key = cache.key("s2", polygon_wgs84.wkt, start.isoformat(), end.isoformat(), max_cloud)
    hit = cache.get_json(key)
    if hit is None:
        try:
            from pystac_client import Client
            cat = Client.open(STAC)
            search = cat.search(collections=["sentinel-2-l2a"],
                                intersects=polygon_wgs84.__geo_interface__,
                                datetime=f"{start.isoformat()}/{end.isoformat()}",
                                query={"eo:cloud_cover": {"lt": max_cloud}}, max_items=200)
            items = sorted(search.items(), key=lambda it: it.datetime)
        except Exception as e:  # noqa: BLE001
            raise DataUnavailable("satellite", str(e)) from e
        seen, hit = set(), []
        for it in items:
            d = it.datetime.date()
            if d in seen:
                continue
            try:
                nd = _field_ndvi(it, polygon_wgs84)
            except Exception:  # noqa: BLE001 -- one bad scene must not sink the series
                nd = None
            if nd is not None:
                seen.add(d)
                hit.append([d.isoformat(), nd])
        cache.put_json(key, hit)
    ser = [(dt.date.fromisoformat(d), nd, fvc(nd)) for d, nd in hit]
    prov = Provenance("Sentinel-2 L2A field-median NDVI (SCL-masked) -> FVC", STAC,
                      "Copernicus open data", today(), S2_ATTRIB, True)
    return ser, prov
