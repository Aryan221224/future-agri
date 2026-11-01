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


def encode_png(rgb: np.ndarray) -> bytes:
    """RGB uint8 (H, W, 3) -> PNG bytes, stdlib only."""
    import struct
    import zlib
    a = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w = a.shape[:2]
    raw = b"".join(b"\x00" + a[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _read_visual(item, polygon_wgs84, margin_m: float):
    import rasterio
    from pyproj import Transformer
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds
    from shapely.ops import transform as shp_transform
    with rasterio.Env(**gdal_env()):
        with rasterio.open(item.assets["visual"].href) as vis:
            fwd = Transformer.from_crs("EPSG:4326", vis.crs, always_xy=True)
            poly = shp_transform(fwd.transform, polygon_wgs84)
            minx, miny, maxx, maxy = poly.buffer(margin_m).bounds
            win = from_bounds(minx, miny, maxx, maxy, vis.transform).round_offsets().round_lengths()
            rgb = vis.read((1, 2, 3), window=win, boundless=True, fill_value=0)
            wt = vis.window_transform(win)
            crs = vis.crs
        h, w = rgb.shape[1:]
        if h < 2 or w < 2 or (rgb.max(axis=0) == 0).mean() > 0.05:
            return None                                     # outside the tile footprint
        with rasterio.open(item.assets["scl"].href) as scl:
            swin = from_bounds(*poly.bounds, scl.transform)
            s = scl.read(1, window=swin, out_shape=(max(2, int(round(swin.height * 2))),
                                                    max(2, int(round(swin.width * 2)))),
                         boundless=True, fill_value=0, resampling=Resampling.nearest)
    if np.isin(s, list(GOOD_SCL)).mean() < 0.8:
        return None                                         # cloud or shadow over the field
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    x0, y0 = wt.c, wt.f
    x1, y1 = x0 + wt.a * w, y0 + wt.e * h
    corners = [inv.transform(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    meta = {"crs": crs.to_string(), "transform": [wt.a, wt.b, wt.c, wt.d, wt.e, wt.f],
            "shape": [h, w], "corners_lonlat": [[round(a, 7), round(b, 7)] for a, b in corners]}
    return np.moveaxis(rgb, 0, -1).astype(np.uint8), meta


def s2_rgb_chip(polygon_wgs84, start: dt.date, end: dt.date, cache: Cache, margin_m: float = 60.0,
                max_cloud: float = 40.0):
    """True-colour chip of the clearest Sentinel-2 scene over the field (L2A ``visual``
    TCI, 10 m) -> (rgb uint8 (H, W, 3), meta, Provenance). ``meta`` has the scene date,
    cloud cover, CRS, affine transform and the corners in lon/lat."""
    import io
    key = cache.key("s2rgb", polygon_wgs84.wkt, start.isoformat(), end.isoformat(), margin_m, max_cloud)
    meta = cache.get_json(key + ".json")
    blob = cache.get_bytes(key + ".npy")
    if meta is None or blob is None:
        try:
            from pystac_client import Client
            search = Client.open(STAC).search(
                collections=["sentinel-2-l2a"], intersects=polygon_wgs84.__geo_interface__,
                datetime=f"{start.isoformat()}/{end.isoformat()}",
                query={"eo:cloud_cover": {"lt": max_cloud}}, max_items=100)
            items = sorted(search.items(), key=lambda it: (it.properties.get("eo:cloud_cover", 100),
                                                           -it.datetime.timestamp()))
        except Exception as e:  # noqa: BLE001
            raise DataUnavailable("satellite", str(e)) from e
        got = None
        for it in items[:10]:
            try:
                got = _read_visual(it, polygon_wgs84, margin_m)
            except Exception:  # noqa: BLE001 -- try the next scene
                got = None
            if got is not None:
                rgb, meta = got
                meta.update(date=it.datetime.date().isoformat(), scene=it.id,
                            cloud=float(it.properties.get("eo:cloud_cover", -1)))
                break
        if got is None:
            raise DataUnavailable("satellite", f"no clear Sentinel-2 scene over the field {start}..{end}")
        buf = io.BytesIO()
        np.save(buf, rgb)
        cache.put_bytes(key + ".npy", buf.getvalue())
        cache.put_json(key + ".json", meta)
        blob = buf.getvalue()
    rgb = np.load(io.BytesIO(blob))
    prov = Provenance("Sentinel-2 L2A true colour (TCI, 10 m)", STAC, "Copernicus open data", today(),
                      S2_ATTRIB, True, f"scene {meta['scene']} on {meta['date']}, cloud {meta['cloud']:.0f}%")
    return rgb, meta, prov
