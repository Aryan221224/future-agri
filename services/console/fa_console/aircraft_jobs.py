"""A spray job (operation) as the fa.job/1 the aircraft flies: the drone twin's passes over
the job's targets (the same generator the KML for Agri Assistant uses), in WGS84, split into
tank-sized sorties, with the field polygon as the fence. Shared by the K3-A Pro agent and the
ArduPilot back-end."""
from __future__ import annotations

import math

import numpy as np

from fa_drone.k3a import generate_route

from .scene import farm_grid

TANK_USE = 0.95


def build_job(farms, sessions, fid: str, op_id: str, home: dict | None = None, height_m: float | None = None,
              speed_ms: float | None = None, transit_height_m: float = 8.0, fence_margin_m: float = 10.0) -> dict:
    from pyproj import Transformer
    s = sessions.get(fid)
    op = s.ops.get(op_id)
    if op is None:
        raise KeyError(f"no operation {op_id}")
    if op.product not in ("herbicide", "fungicide"):
        raise ValueError(f"operation {op_id} is a {op.product} job: nothing to spray")
    vf = farms.load(fid)
    grid = farm_grid(vf)
    flight = s.flight if not op.settings else type(s.flight)(**op.settings)
    targets = None if op.targets is None else np.asarray(op.targets, bool)
    route = generate_route(grid, flight, s.spec, targets=targets)
    if not route.passes:
        raise ValueError("the job has no passes (no target cells)")
    to_ll = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    ll = lambda x, y: [round(v, 8) for v in to_ll.transform(x, y)]  # noqa: E731
    if home is None:                                   # the field edge next to the first pass
        from shapely.geometry import Point
        p0 = Point(route.passes[0].start)
        b = grid.polygon_local.exterior
        q = b.interpolate(b.project(p0))
        hx, hy = q.x - 3.0 * (q.x - grid.polygon_local.centroid.x) / max(1e-6, q.distance(grid.polygon_local.centroid)), \
            q.y - 3.0 * (q.y - grid.polygon_local.centroid.y) / max(1e-6, q.distance(grid.polygon_local.centroid))
        lon, lat = to_ll.transform(hx, hy)
        home = {"lat": lat, "lon": lon}
    tank = float(getattr(s.spec, "tank_l", 10.0)) * TANK_USE
    rate, swath = flight.rate_l_ha, flight.swath_m
    sorties, cur, litres = [], [], 0.0
    for p in route.passes:
        need = p.length * swath * rate / 1e4
        if cur and litres + need > tank:
            sorties.append(cur)
            cur, litres = [], 0.0
        cur.append({"points": [ll(*p.start), ll(*p.end)], "spray": True})
        litres += need
    if cur:
        sorties.append(cur)
    fence = [ll(x, y) for x, y in list(grid.polygon_local.exterior.coords)[:-1]]
    return {"schema": "fa.job/1", "job_id": f"{fid}:{op_id}", "home": home, "fence": fence,
            "fence_margin_m": fence_margin_m, "ceiling_m": 30.0,
            "height_m": height_m or float(flight.height_m), "transit_height_m": transit_height_m,
            "speed_ms": speed_ms or float(flight.speed_ms), "transit_speed_ms": 6.0, "sorties": sorties,
            "summary": {"passes": len(route.passes), "sorties": len(sorties),
                        "spray_m": round(route.spray_length_m, 1),
                        "litres": round(route.spray_length_m * swath * rate / 1e4, 2), "rate_l_ha": rate,
                        "swath_m": swath, "angle_deg": round(route.angle_deg, 1)}}


def job_distance_ok(job: dict) -> None:
    """Refuse a job whose home is far from the field (a wrong home = a long, wrong flight)."""
    from fa_agent.route import Job
    Job.from_json(job)                                 # validates fence, heights, speeds, distance
    h = job["home"]
    lon0, lat0 = job["fence"][0]
    d = math.hypot((lon0 - h["lon"]) * 111_320 * math.cos(math.radians(lat0)), (lat0 - h["lat"]) * 111_320)
    if d > 2000:
        raise ValueError(f"home is {d:.0f} m from the field: set the take-off point next to the field")
