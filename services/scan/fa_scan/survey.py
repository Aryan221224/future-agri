"""Survey plan for the separate camera drone: altitude from the target GSD,
line spacing and trigger distance from overlap, frames, flight time, batteries.

Ground speed is capped by the camera's trigger interval (forward overlap) and by
motion blur (<= half a pixel during the exposure).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from fa_core.geo import CAMERAS, plan_survey


@dataclass
class ScanPlan:
    camera: str
    gsd_m: float
    altitude_m: float
    line_spacing_m: float
    trigger_distance_m: float
    speed_ms: float
    frames: int
    flight_min: float
    batteries: int
    front_overlap: float
    side_overlap: float


def plan_scan(grid, camera_key: str = "p4rtk", gsd_m: float = 0.01, front: float = 0.80,
              side: float = 0.70, max_speed_ms: float = 12.0, trigger_interval_s: float = 2.0,
              exposure_s: float = 1 / 1000, endurance_min: float = 25.0,
              turn_s: float = 6.0) -> ScanPlan:
    cam = CAMERAS[camera_key]
    sp = plan_survey(cam, gsd_m, front, side)
    v = min(max_speed_ms, sp.trigger_distance_m / trigger_interval_s, 0.5 * gsd_m / exposure_s)
    v = max(v, 1.0)
    minx, miny, maxx, maxy = grid.bounds_local
    w, h = maxx - minx, maxy - miny
    lines = max(1, math.ceil(min(w, h) / sp.line_spacing_m) + 1)
    length = lines * max(w, h)
    t_min = (length / v + lines * turn_s + 2 * sp.altitude_m / 3.0) / 60.0
    return ScanPlan(camera_key, gsd_m, sp.altitude_m, sp.line_spacing_m, sp.trigger_distance_m, v,
                    sp.frames_for_area(grid.area_ha * 1e4), round(t_min, 2),
                    max(1, math.ceil(t_min / (0.8 * endurance_min))), front, side)
