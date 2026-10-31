"""JIYI K3-A Pro behaviour: how the flight controller and Agri Assistant turn a
field polygon into a flown route.

The real controller cannot be run (no SDK, no SITL), so its *behaviour* is
emulated: parallel passes at the set spacing aligned with the field's long edge
(the app default), serpentine order, polygon routes for spot treatment, and the
400-waypoint limit per route group (larger jobs are split into groups, as the
app requires). Mode limits (speed gears, 30 deg tilt, 6 m/s vertical) are
applied by the mission simulator.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from fa_planner.coverage import order_boustrophedon, plan_coverage

from .spec import DroneSpec


class RouteTooLarge(ValueError):
    pass


@dataclass
class Pass:
    start: tuple[float, float]
    end: tuple[float, float]

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def unit(self) -> tuple[float, float]:
        L = max(self.length, 1e-9)
        return ((self.end[0] - self.start[0]) / L, (self.end[1] - self.start[1]) / L)

    @property
    def normal(self) -> tuple[float, float]:
        ux, uy = self.unit
        return (-uy, ux)


@dataclass
class Route:
    groups: list[list[Pass]]
    angle_deg: float
    target_polygons: list = field(default_factory=list)

    @property
    def passes(self) -> list[Pass]:
        return [p for g in self.groups for p in g]

    @property
    def n_waypoints(self) -> int:
        return 2 * len(self.passes)

    @property
    def spray_length_m(self) -> float:
        return sum(p.length for p in self.passes)


def app_default_angle(poly) -> float:
    """Agri Assistant aligns passes with the field's long edge: the long side of
    the minimum rotated rectangle."""
    mrr = poly.minimum_rotated_rectangle
    pts = list(mrr.exterior.coords)[:4]
    edges = [(math.dist(pts[i], pts[(i + 1) % 4]), pts[i], pts[(i + 1) % 4]) for i in range(4)]
    _, a, b = max(edges, key=lambda e: e[0])
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0


def target_polygons(grid, targets: np.ndarray, pad_m: float) -> list:
    """Union of target cells (squares) padded by ``pad_m`` -> list of polygons,
    clipped to the field."""
    xy = grid.cells_xy[np.asarray(targets, dtype=bool)]
    if len(xy) == 0:
        return []
    h = grid.cell_m / 2
    u = unary_union([box(x - h, y - h, x + h, y + h) for x, y in xy]).buffer(pad_m)
    u = u.intersection(grid.polygon_local)
    geoms = list(u.geoms) if hasattr(u, "geoms") else [u]
    return [g for g in geoms if isinstance(g, Polygon) and g.area > 1.0]


def _passes_for(poly, spacing: float, angle: float) -> list[Pass]:
    plan = plan_coverage(poly, spacing, angle_deg=angle, prefer_f2c=False)
    if not plan.swaths:
        # narrower than one spacing: a single centre-line pass along the long axis
        mrr = poly.minimum_rotated_rectangle
        c = poly.centroid
        a = math.radians(angle)
        L = max(math.sqrt(mrr.area) * 2, 2.0)
        ln = LineString([(c.x - L * math.cos(a), c.y - L * math.sin(a)),
                         (c.x + L * math.cos(a), c.y + L * math.sin(a))]).intersection(poly)
        if ln.is_empty or ln.length < 0.5:
            return []
        segs = list(ln.geoms) if hasattr(ln, "geoms") else [ln]
        return [Pass(tuple(s.coords[0]), tuple(s.coords[-1])) for s in segs if s.length >= 0.5]
    out = []
    for s in order_boustrophedon(plan.swaths):
        c = list(s.line.coords)
        out.append(Pass(tuple(c[0]), tuple(c[-1])))
    return out


def generate_route(grid, settings, spec: DroneSpec, targets: np.ndarray | None = None,
                   split_groups: bool = True) -> Route:
    poly = grid.polygon_local
    angle = settings.route_angle_deg if settings.route_angle_deg is not None else app_default_angle(poly)
    if targets is None:
        areas = [poly]
    else:
        areas = target_polygons(grid, targets, pad_m=settings.swath_m / 2)
    passes: list[Pass] = []
    for a in areas:
        passes.extend(_passes_for(a, settings.swath_m, angle))
    per_group = max(1, spec.fc.max_waypoints_per_group // 2)
    if len(passes) > per_group and not split_groups:
        raise RouteTooLarge(f"{2 * len(passes)} waypoints > {spec.fc.max_waypoints_per_group}")
    groups = [passes[i:i + per_group] for i in range(0, len(passes), per_group)] or []
    return Route(groups, angle, areas if targets is not None else [])
