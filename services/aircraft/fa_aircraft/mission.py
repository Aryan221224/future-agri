"""A spray job (schema fa.job/1, the same one fa-agent flies on the K3-A Pro) as an ArduPilot
Copter mission and geofence.

Mission, one sortie:
    0  home (ArduPilot overwrites it)
    1  NAV_TAKEOFF to the transit height
    2  DO_CHANGE_SPEED transit speed
    per pass: WAYPOINT lead-in at spray height (sprayer off) -> DO_CHANGE_SPEED spray speed
              -> DO_SPRAYER on -> WAYPOINT along the pass ... -> DO_SPRAYER off
              -> DO_CHANGE_SPEED transit (when the next pass is far)
    last NAV_RETURN_TO_LAUNCH (lands at home)
Fence: the field polygon (inclusion) plus the job's margin, from FENCE_MARGIN / the polygon.
Altitudes are relative to home (MAV_FRAME_GLOBAL_RELATIVE_ALT_INT).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from pymavlink import mavutil

M = mavutil.mavlink
FRAME_REL = M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
FRAME_MISSION = M.MAV_FRAME_MISSION
_FRAME_EQ = {M.MAV_FRAME_GLOBAL_INT: M.MAV_FRAME_GLOBAL, M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT:
             M.MAV_FRAME_GLOBAL_RELATIVE_ALT, M.MAV_FRAME_GLOBAL_TERRAIN_ALT_INT: M.MAV_FRAME_GLOBAL_TERRAIN_ALT}


@dataclass
class Item:
    command: int
    frame: int = FRAME_REL
    p1: float = 0.0
    p2: float = 0.0
    p3: float = 0.0
    p4: float = 0.0
    lat: float = 0.0
    lon: float = 0.0
    alt: float = 0.0

    def msg(self, target_sys: int, target_comp: int, seq: int, mission_type: int = 0):
        return M.MAVLink_mission_item_int_message(
            target_sys, target_comp, seq, self.frame, self.command, 0, 1, self.p1, self.p2, self.p3, self.p4,
            int(round(self.lat * 1e7)), int(round(self.lon * 1e7)), float(self.alt), mission_type)

    def same_as(self, m) -> bool:
        """Does a downloaded MISSION_ITEM_INT match this item, as ArduPilot stores it? It
        normalises the *_INT frames to their float twins and does not keep a waypoint's
        acceptance radius (Copter uses WPNAV_RADIUS), so those are not compared."""
        if m.command != self.command:
            return False
        positional = self.frame != FRAME_MISSION          # DO_* / RTL: ArduPilot reports frame 0, no position
        if positional and _FRAME_EQ.get(m.frame, m.frame) != _FRAME_EQ.get(self.frame, self.frame):
            return False
        if positional and (abs(m.x - round(self.lat * 1e7)) > 1 or
                                            abs(m.y - round(self.lon * 1e7)) > 1 or abs(m.z - self.alt) > 0.01):
            return False
        pairs = [(m.param1, self.p1)]
        if self.command == M.MAV_CMD_DO_CHANGE_SPEED:
            pairs.append((m.param2, self.p2))
        return all(abs(a - b) < 1e-3 for a, b in pairs)


def wp(lon: float, lat: float, alt: float, accept_m: float = 0.5) -> Item:
    return Item(M.MAV_CMD_NAV_WAYPOINT, FRAME_REL, 0.0, accept_m, 0.0, float("nan"), lat, lon, alt)


def speed(ms: float) -> Item:
    return Item(M.MAV_CMD_DO_CHANGE_SPEED, FRAME_MISSION, 1.0, ms, -1.0, 0.0)


def sprayer(on: bool) -> Item:
    return Item(M.MAV_CMD_DO_SPRAYER, FRAME_MISSION, 1.0 if on else 0.0)


def sortie_items(job, sortie: int, lead_in_m: float = 4.0) -> list[Item]:
    """MAVLink items for one sortie of a fa_agent.route.Job."""
    loc = job.local
    hlon, hlat = loc.to_lonlat(0.0, 0.0)
    items = [Item(M.MAV_CMD_NAV_WAYPOINT, M.MAV_FRAME_GLOBAL, lat=hlat, lon=hlon, alt=0.0),
             Item(M.MAV_CMD_NAV_TAKEOFF, FRAME_REL, alt=job.transit_height_m, lat=hlat, lon=hlon),
             speed(job.transit_speed_ms)]
    prev_end = (0.0, 0.0)
    for k, p in enumerate(job.sorties[sortie]):
        (ax, ay), (bx, by) = p.points[0], p.points[1]
        d = math.hypot(bx - ax, by - ay) or 1.0
        pre = (ax - lead_in_m * (bx - ax) / d, ay - lead_in_m * (by - ay) / d)
        far = math.dist(prev_end, pre) > 30.0
        if far:
            items.append(wp(*loc.to_lonlat(*pre), job.transit_height_m, accept_m=1.0))
        items.append(wp(*loc.to_lonlat(*pre), job.height_m, accept_m=0.5))
        items.append(speed(job.speed_ms))
        items.append(wp(*loc.to_lonlat(*p.points[0]), job.height_m, accept_m=0.3))
        if p.spray:
            items.append(sprayer(True))
        for pt in p.points[1:]:
            items.append(wp(*loc.to_lonlat(*pt), job.height_m, accept_m=0.3))
        if p.spray:
            items.append(sprayer(False))
        items.append(speed(job.transit_speed_ms))
        prev_end = p.points[-1]
    items.append(Item(M.MAV_CMD_NAV_RETURN_TO_LAUNCH, FRAME_MISSION))
    return items


def fence_polygon(job, home_radius_m: float = 10.0, max_vertices: int = 60) -> list[tuple[float, float]]:
    """Inclusion fence in local metres: the field plus the job's margin, joined to the take-off
    point (ArduPilot refuses to arm outside its fence). Convex, simplified to max_vertices."""
    from shapely.geometry import Point, Polygon
    field = Polygon(job.fence).buffer(job.fence_margin_m)
    area = field.union(Point(0.0, 0.0).buffer(home_radius_m)).convex_hull
    tol = 0.5
    while len(area.exterior.coords) - 1 > max_vertices:
        area = area.simplify(tol)
        tol *= 2
    return list(area.exterior.coords)[:-1]


def fence_items(job) -> list[Item]:
    """The fence as inclusion-polygon vertices (MAV_MISSION_TYPE_FENCE)."""
    pts = [job.local.to_lonlat(*p) for p in fence_polygon(job)]
    n = len(pts)
    return [Item(M.MAV_CMD_NAV_FENCE_POLYGON_VERTEX_INCLUSION, M.MAV_FRAME_GLOBAL, float(n), lat=lat, lon=lon)
            for lon, lat in pts]
