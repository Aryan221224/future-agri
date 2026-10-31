"""Coverage path planning over real field polygons.

Fields2Cover is the right dependency for this and is used when importable: it
is BSD-3, actively released, and implements headland generation, swath
generation, route ordering and feasible Dubins/Reeds-Shepp turns properly. But
it ships as an sdist that compiles the whole C++ library against GDAL, GEOS and
OR-Tools C++, which is the single most fragile step in the whole stack. So this
module has a pure-Shapely fallback implementing the same boustrophedon
decomposition, and the system runs either way.

What Fields2Cover does *not* do, in either case, is energy: its ``Robot`` type
exposes width, speed and turning radius and has no battery, capacity or refill
concept anywhere. Multi-sortie decomposition under an endurance limit is ours to
write, and it lives in ``routing.py``.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Polygon, MultiPolygon, shape
from shapely.ops import unary_union

log = logging.getLogger(__name__)

try:  # pragma: no cover - exercised only where the C++ build succeeded
    import fields2cover as f2c  # type: ignore
    HAVE_F2C = True
except Exception:
    f2c = None
    HAVE_F2C = False


@dataclass
class Swath:
    """One pass across the field."""
    index: int
    line: LineString
    length_m: float

    @property
    def start(self) -> tuple[float, float]:
        return self.line.coords[0]

    @property
    def end(self) -> tuple[float, float]:
        return self.line.coords[-1]

    def reversed(self) -> "Swath":
        return Swath(self.index, LineString(list(self.line.coords)[::-1]), self.length_m)


@dataclass
class CoveragePlan:
    swaths: list[Swath]
    headland: Polygon | MultiPolygon | None
    working_area: Polygon | MultiPolygon
    angle_deg: float
    swath_width_m: float
    backend: str
    field_area_m2: float = 0.0

    @property
    def total_swath_length_m(self) -> float:
        return sum(s.length_m for s in self.swaths)

    @property
    def covered_area_m2(self) -> float:
        return self.total_swath_length_m * self.swath_width_m

    @property
    def working_area_m2(self) -> float:
        return float(self.working_area.area)

    @property
    def headland_area_m2(self) -> float:
        return float(self.headland.area) if self.headland is not None else 0.0

    @property
    def coverage_ratio(self) -> float:
        """Covered area over *working* area.

        Measured against the working area, not the whole field: the headland is
        by construction not covered by these swaths and needs its own passes, so
        dividing by field area conflates "swaths are badly spaced" with "the
        headland is large". Values above 1 mean genuine overlap -- swaths
        covering the same ground twice -- which is the real cost of a poor angle
        and is what ``best_angle`` minimises.
        """
        if self.working_area_m2 <= 0:
            return float("nan")
        return self.covered_area_m2 / self.working_area_m2


def _clip_lines(geom, angle_deg: float, spacing: float) -> list[LineString]:
    """Parallel lines at ``angle_deg`` clipped to the geometry."""
    c = geom.centroid
    rot = affinity.rotate(geom, -angle_deg, origin=c)
    minx, miny, maxx, maxy = rot.bounds
    lines: list[LineString] = []
    y = miny + spacing / 2.0
    while y < maxy:
        seg = LineString([(minx - 1.0, y), (maxx + 1.0, y)])
        inter = seg.intersection(rot)
        if not inter.is_empty:
            parts = (list(inter.geoms) if isinstance(inter, MultiLineString) else [inter])
            for p in parts:
                if isinstance(p, LineString) and p.length > 1e-6:
                    lines.append(affinity.rotate(p, angle_deg, origin=c))
        y += spacing
    return lines


def plan_coverage(field_geom, swath_width_m: float, angle_deg: float | None = None,
                  headland_passes: int = 0, prefer_f2c: bool = True) -> CoveragePlan:
    """Generate swaths covering ``field_geom`` (a projected, metric geometry).

    ``field_geom`` must already be in a metre-based CRS -- see fa_core.geo.to_local.
    Planning in degrees silently produces swath spacings that are wrong by a
    factor of ~10^5.

    ``headland_passes`` defaults to 0, which is correct for a multirotor and
    wrong for a tractor. Headland exists because a ground vehicle needs room to
    turn around at the end of a pass; a multirotor stops, yaws and reverses in
    place. Reserving a swath-width headland on a 300x200 m field removes a
    third of the working area for no reason. Set it to 1 only for
    fixed-wing aircraft or ground vehicles with a real turning radius.
    """
    geom = shape(field_geom) if isinstance(field_geom, dict) else field_geom
    if geom.is_empty:
        raise ValueError("empty field geometry")
    if not geom.is_valid:
        geom = geom.buffer(0)
    field_area = geom.area

    if angle_deg is None:
        angle_deg = best_angle(geom, swath_width_m)

    headland_width = headland_passes * swath_width_m
    working = geom.buffer(-headland_width) if headland_width > 0 else geom
    if working.is_empty:
        log.warning("headland of %.1f m consumes the whole field; planning without one",
                    headland_width)
        working, headland = geom, None
    else:
        headland = geom.difference(working)

    if prefer_f2c and HAVE_F2C:
        try:
            return _plan_with_f2c(geom, working, headland, swath_width_m, angle_deg, field_area)
        except Exception as e:  # pragma: no cover
            log.warning("Fields2Cover backend failed (%s); using shapely fallback", e)

    lines = _clip_lines(working, angle_deg, swath_width_m)
    swaths = [Swath(i, ln, ln.length) for i, ln in enumerate(lines)]
    return CoveragePlan(swaths, headland, working, angle_deg, swath_width_m,
                        "shapely", field_area)


def _plan_with_f2c(geom, working, headland, swath_width_m, angle_deg, field_area):  # pragma: no cover
    """Fields2Cover path. Kept deliberately thin: we take its swath geometry and
    do our own routing, because F2C's route planner has no energy model."""
    ring = list(working.exterior.coords)
    f2c_ring = f2c.LinearRing()
    for x, y in ring:
        f2c_ring.addPoint(f2c.Point(float(x), float(y)))
    cell = f2c.Cell(f2c_ring)
    cells = f2c.Cells(cell)
    robot = f2c.Robot(float(swath_width_m), float(swath_width_m))
    bf = f2c.SG_BruteForce()
    swaths_f2c = bf.generateBestSwaths(f2c.OBJ_NSwath(), robot.getCovWidth(), cells)
    out: list[Swath] = []
    for i in range(swaths_f2c.size()):
        s = swaths_f2c.at(i)
        p0, p1 = s.startPoint(), s.endPoint()
        ln = LineString([(p0.getX(), p0.getY()), (p1.getX(), p1.getY())])
        out.append(Swath(i, ln, ln.length))
    return CoveragePlan(out, headland, working, angle_deg, swath_width_m,
                        "fields2cover", field_area)


def best_angle(geom, swath_width_m: float, step_deg: float = 5.0) -> float:
    """Swath angle minimising the number of turns.

    Turns cost time and energy and produce nothing, so the objective is the
    swath count -- with total swath length as the tiebreak. Sweeping 0-180 at
    5 degrees is cheap and beats the common heuristic of "align to the longest
    edge", which is wrong for any field that is not convex.
    """
    best, best_key = 0.0, None
    for a in np.arange(0.0, 180.0, step_deg):
        lines = _clip_lines(geom, float(a), swath_width_m)
        if not lines:
            continue
        key = (len(lines), sum(l.length for l in lines))
        if best_key is None or key < best_key:
            best, best_key = float(a), key
    return best


def order_boustrophedon(swaths: list[Swath]) -> list[Swath]:
    """Serpentine ordering: alternate direction so consecutive swaths connect end-to-start."""
    out: list[Swath] = []
    for i, s in enumerate(sorted(swaths, key=lambda s: s.index)):
        out.append(s if i % 2 == 0 else s.reversed())
    return out


def path_length_m(ordered: list[Swath], home: tuple[float, float] | None = None) -> float:
    """Total flown distance including turns and the legs to and from home."""
    if not ordered:
        return 0.0
    total = sum(s.length_m for s in ordered)
    for a, b in zip(ordered, ordered[1:]):
        total += math.dist(a.end, b.start)
    if home is not None:
        total += math.dist(home, ordered[0].start) + math.dist(ordered[-1].end, home)
    return total
