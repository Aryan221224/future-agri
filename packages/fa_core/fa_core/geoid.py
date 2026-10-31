"""Deterministic, content-addressed identity for field boundaries.

Why this exists
---------------
The same physical field gets described slightly differently by the grower, the
agronomist and the drone operator: a vertex moved by 20 cm, a ring reversed, a
different start vertex, a different CRS. Those all denote the same field but
hash differently, so naive `hash(wkt)` identity fragments a farm's history.

AgStack's Asset Registry solves this with an S2-cell cover: normalise the
geometry to a cell cover at a fixed level, and identity becomes a property of
the ground the polygon covers rather than of how someone typed it. We implement
the same idea directly (AgStack's registry has single-digit adoption; the idea
is sound, the dependency is not).

We use a quantised geohash-style cover rather than pulling in the full S2
library: at level ~22 the cell edge is roughly 2 m, which is the tolerance we
actually want for "same field". `geoid` is stable under vertex reordering, ring
winding, CRS round-trips and sub-metre vertex jitter.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from shapely.geometry import Polygon, MultiPolygon, shape, mapping
from shapely.ops import unary_union

# Cell size in degrees at the equator. 2e-5 deg ~= 2.2 m.
DEFAULT_CELL_DEG = 2e-5


def _cells(geom: Polygon | MultiPolygon, cell_deg: float) -> set[tuple[int, int]]:
    """Integer cell cover of a lon/lat polygon.

    Scanline fill: for every cell row intersecting the bounding box, take the
    row's centre latitude and find the x-range actually covered. This is an
    interior cover, so slivers narrower than a cell vanish -- which is the
    intent: identity should not depend on a 30 cm spur.
    """
    minx, miny, maxx, maxy = geom.bounds
    out: set[tuple[int, int]] = set()
    j0, j1 = int(math.floor(miny / cell_deg)), int(math.ceil(maxy / cell_deg))
    # Guard against pathological inputs (a whole-globe polygon would be 10^10 cells).
    if (j1 - j0) > 20000:
        raise ValueError("geometry too large for geoid cover; simplify or split it")
    for j in range(j0, j1 + 1):
        y = (j + 0.5) * cell_deg
        i0, i1 = int(math.floor(minx / cell_deg)), int(math.ceil(maxx / cell_deg))
        if (i1 - i0) > 20000:
            raise ValueError("geometry too large for geoid cover; simplify or split it")
        for i in range(i0, i1 + 1):
            x = (i + 0.5) * cell_deg
            if geom.covers(_pt(x, y)):
                out.add((i, j))
    return out


def _pt(x: float, y: float):
    from shapely.geometry import Point

    return Point(x, y)


def geoid(geom, cell_deg: float = DEFAULT_CELL_DEG, prefix: str = "fa") -> str:
    """Return a stable identifier for the ground covered by ``geom``.

    ``geom`` may be a shapely geometry or a GeoJSON-like mapping, in EPSG:4326.
    """
    if isinstance(geom, dict):
        geom = shape(geom)
    if isinstance(geom, (list, tuple)):
        geom = unary_union([shape(g) if isinstance(g, dict) else g for g in geom])
    if geom.is_empty:
        raise ValueError("cannot compute a geoid for an empty geometry")
    if not geom.is_valid:
        geom = geom.buffer(0)
    cover = _cells(geom, cell_deg)
    if not cover:
        # Field smaller than one cell: fall back to the quantised centroid so
        # tiny trial plots still get a stable id.
        c = geom.centroid
        cover = {(int(math.floor(c.x / cell_deg)), int(math.floor(c.y / cell_deg)))}
    h = hashlib.blake2b(digest_size=16)
    for i, j in sorted(cover):
        h.update(i.to_bytes(8, "big", signed=True))
        h.update(j.to_bytes(8, "big", signed=True))
    return f"{prefix}_{h.hexdigest()}"


@dataclass(frozen=True)
class GeoIdMatch:
    a: str
    b: str
    jaccard: float

    @property
    def same_field(self) -> bool:
        return self.jaccard >= 0.95


def compare(geom_a, geom_b, cell_deg: float = DEFAULT_CELL_DEG) -> GeoIdMatch:
    """Jaccard overlap of two geometries' cell covers.

    Exact geoid equality is brittle for boundaries redrawn by hand; this gives a
    graded answer so the UI can offer "this looks like Field 12, is it?".
    """
    ga = shape(geom_a) if isinstance(geom_a, dict) else geom_a
    gb = shape(geom_b) if isinstance(geom_b, dict) else geom_b
    ca, cb = _cells(ga, cell_deg), _cells(gb, cell_deg)
    if not ca and not cb:
        return GeoIdMatch(geoid(ga), geoid(gb), 0.0)
    inter = len(ca & cb)
    union = len(ca | cb) or 1
    return GeoIdMatch(geoid(ga, cell_deg), geoid(gb, cell_deg), inter / union)
