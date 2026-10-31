"""Real field boundaries anywhere: Fields of The World global predictions
(PRUE model on Sentinel-2, 2024-2025), GeoParquet partitioned by country,
CC-BY-4.0. Only the row groups whose bbox statistics touch the point are read,
so a 2 GB country file costs a few megabytes.
"""
from __future__ import annotations

import re

import numpy as np

from .base import Cache, DataUnavailable, Provenance, today

FTW_BASE = ("https://data.source.coop/ftw/global-data/predictions/vectors/alpha/"
            "results-by-admin-conf")
FTW_LIST = "https://data.source.coop/ftw/global-data"
FTW_ATTRIB = ("Fields of The World global field boundaries (PRUE; Taylor Geospatial Institute and "
              "Microsoft AI for Good Research Lab), CC-BY-4.0")


class NoFieldFound(LookupError):
    pass


def choose_field(candidates: list[dict], lat: float, lon: float, min_ha: float, max_ha: float,
                 min_conf: float = 69.0):
    """Nearest confident polygon of the right size -> (shapely Polygon WGS84, attrs)."""
    import shapely
    from shapely.geometry import Point
    from fa_core.geo import area_ha
    pt = Point(lon, lat)
    best = None
    for c in candidates:
        conf = c.get("confidence")
        if conf is None or conf < min_conf:
            continue
        a = c["area_m2"] / 1e4
        if not (min_ha <= a <= max_ha):
            continue
        g = shapely.from_wkb(bytes.fromhex(c["wkb_hex"]))
        if g.geom_type == "MultiPolygon":
            g = max(g.geoms, key=lambda p: p.area)
        if not g.is_valid:
            g = g.buffer(0)
        d = g.distance(pt)
        if best is None or d < best[0]:
            best = (d, g, c)
    if best is None:
        raise NoFieldFound(f"no FTW field {min_ha}-{max_ha} ha with confidence >= {min_conf} "
                           f"near ({lat:.4f}, {lon:.4f})")
    _, g, c = best
    return g, {"id": c["id"], "confidence": c["confidence"], "area_ha": area_ha(g),
               "distance_deg": float(best[0])}


def country_files(country: str, cache: Cache) -> list[str]:
    key = cache.key("ftw-list", country)
    hit = cache.get_json(key)
    if hit is not None:
        return hit
    from .base import session
    prefix = f"predictions/vectors/alpha/results-by-admin-conf/admin:country_code={country}/"
    try:
        r = session().get(FTW_LIST, params={"list-type": "2", "prefix": prefix}, timeout=60)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        raise DataUnavailable("field boundaries", str(e)) from e
    files = [k for k in re.findall(r"<Key>([^<]+)</Key>", r.text) if k.endswith(".parquet")]
    if not files:
        raise DataUnavailable("field boundaries", f"no FTW partition for country {country}")
    cache.put_json(key, files)
    return files


def candidates_near(lat: float, lon: float, country: str, cache: Cache,
                    radius_deg: float = 0.02) -> list[dict]:
    key = cache.key("ftw-cands", round(lat, 4), round(lon, 4), country, radius_deg)
    hit = cache.get_json(key)
    if hit is not None:
        return hit
    import fsspec
    import pyarrow.parquet as pq
    fs = fsspec.filesystem("https", client_kwargs={"trust_env": True})
    out: list[dict] = []
    d = radius_deg
    try:
        for f in country_files(country, cache):
            url = ("https://data.source.coop/ftw/" + f if f.startswith("global-data/")
                   else "https://data.source.coop/ftw/global-data/" + f)
            pf = pq.ParquetFile(fs.open(url, block_size=2 ** 20, cache_type="readahead"))
            md = pf.metadata
            for i in range(md.num_row_groups):
                rg = md.row_group(i)
                st = {rg.column(j).path_in_schema: rg.column(j).statistics
                      for j in range(rg.num_columns)}
                if any(st.get(k) is None or not st[k].has_min_max
                       for k in ("bbox.xmin", "bbox.xmax", "bbox.ymin", "bbox.ymax")):
                    continue
                if (st["bbox.xmin"].min > lon + d or st["bbox.xmax"].max < lon - d
                        or st["bbox.ymin"].min > lat + d or st["bbox.ymax"].max < lat - d):
                    continue
                bb = pf.read_row_group(i, columns=["bbox"]).column("bbox").to_pylist()
                hits = [k for k, b in enumerate(bb) if b["xmin"] < lon + d and b["xmax"] > lon - d
                        and b["ymin"] < lat + d and b["ymax"] > lat - d]
                if not hits:
                    continue
                t = pf.read_row_group(i, columns=["id", "confidence", "metrics:area", "geometry"])
                ids, conf = t.column("id").to_pylist(), t.column("confidence").to_pylist()
                area, geom = t.column("metrics:area").to_pylist(), t.column("geometry")
                for k in hits:
                    out.append({"id": ids[k], "confidence": conf[k], "area_m2": area[k],
                                "wkb_hex": geom[k].as_py().hex()})
    except DataUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise DataUnavailable("field boundaries", str(e)) from e
    cache.put_json(key, out)
    return out


def field_near(lat: float, lon: float, country: str, cache: Cache, min_ha: float,
               max_ha: float, min_conf: float = 69.0, radius_deg: float = 0.02):
    """A real field polygon near (lat, lon) -> (Polygon WGS84, attrs, Provenance).
    The search radius doubles (up to 0.16 deg) until a matching field is found."""
    r = radius_deg
    while True:
        cands = candidates_near(lat, lon, country, cache, r)
        try:
            poly, attrs = choose_field(cands, lat, lon, min_ha, max_ha, min_conf)
            break
        except NoFieldFound:
            if r >= 0.16:
                raise
            r *= 2
    prov = Provenance("Fields of The World global predictions (PRUE)", FTW_BASE, "CC-BY-4.0",
                      today(), FTW_ATTRIB, True,
                      f"field id {attrs['id']}, confidence {attrs['confidence']:.0f}/100 "
                      "(cell-level reliability, not polygon accuracy)")
    return poly, attrs, prov


def polygon_from_geojson(gj: dict):
    """User-supplied boundary (GeoJSON Feature/Polygon) -> shapely Polygon WGS84."""
    from shapely.geometry import shape
    g = gj.get("geometry", gj)
    p = shape(g)
    if p.geom_type == "MultiPolygon":
        p = max(p.geoms, key=lambda q: q.area)
    if p.geom_type != "Polygon":
        raise ValueError(f"boundary must be a Polygon, got {p.geom_type}")
    return p if p.is_valid else p.buffer(0)


_ = np  # numpy used by callers via this module's namespace in tests
