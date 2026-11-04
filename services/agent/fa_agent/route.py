"""A spray job as the console sends it, in local metres around home.

Job JSON (schema ``fa.job/1``):
    {"schema": "fa.job/1", "job_id": "...", "home": {"lat": .., "lon": ..},
     "fence": [[lon, lat], ...],            # the field polygon
     "fence_margin_m": 10, "ceiling_m": 30,
     "height_m": 2.5, "transit_height_m": 8, "speed_ms": 5, "transit_speed_ms": 6,
     "sorties": [[{"points": [[lon, lat], ...], "spray": true}, ...], ...]}
Positions are east/north metres from home (a local tangent plane: under 1 mm error over
a few km), heights are metres above the take-off point.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

R_EARTH = 6_378_137.0
SCHEMA = "fa.job/1"


@dataclass
class Local:
    lat0: float
    lon0: float

    def to_en(self, lon: float, lat: float) -> tuple[float, float]:
        e = math.radians(lon - self.lon0) * R_EARTH * math.cos(math.radians(self.lat0))
        n = math.radians(lat - self.lat0) * R_EARTH
        return e, n

    def to_lonlat(self, e: float, n: float) -> tuple[float, float]:
        lat = self.lat0 + math.degrees(n / R_EARTH)
        lon = self.lon0 + math.degrees(e / (R_EARTH * math.cos(math.radians(self.lat0))))
        return lon, lat


@dataclass
class Pass:
    points: list[tuple[float, float]]       # local metres
    spray: bool = True

    @property
    def length_m(self) -> float:
        return sum(math.dist(a, b) for a, b in zip(self.points, self.points[1:]))


@dataclass
class Job:
    job_id: str
    local: Local
    fence: list[tuple[float, float]]
    sorties: list[list[Pass]]
    fence_margin_m: float = 10.0
    ceiling_m: float = 30.0
    height_m: float = 2.5
    transit_height_m: float = 8.0
    speed_ms: float = 5.0
    transit_speed_ms: float = 6.0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: dict) -> "Job":
        if d.get("schema") != SCHEMA:
            raise ValueError(f"job schema must be {SCHEMA}")
        try:
            home = d["home"]
            loc = Local(float(home["lat"]), float(home["lon"]))
            fence = [loc.to_en(float(x), float(y)) for x, y in d["fence"]]
            sorties = [[Pass([loc.to_en(float(x), float(y)) for x, y in p["points"]], bool(p.get("spray", True)))
                        for p in s] for s in d["sorties"]]
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"malformed job: {e}") from e
        job = cls(str(d.get("job_id", "job")), loc, fence, sorties,
                  **{k: float(d[k]) for k in ("fence_margin_m", "ceiling_m", "height_m", "transit_height_m",
                                                "speed_ms", "transit_speed_ms") if k in d}, raw=d)
        job.validate()
        return job

    def validate(self) -> None:
        if len(self.fence) < 3:
            raise ValueError("the fence needs at least 3 points")
        if not self.sorties or not all(self.sorties):
            raise ValueError("a job needs at least one sortie with one pass")
        for s in self.sorties:
            for p in s:
                if len(p.points) < 2:
                    raise ValueError("a pass needs at least 2 points")
        if not 0.5 <= self.height_m <= 15 or not 2 <= self.transit_height_m <= self.ceiling_m <= 120:
            raise ValueError("heights out of range (spray 0.5-15 m, transit 2 m to the ceiling, ceiling <= 120 m)")
        if not 0.5 <= self.speed_ms <= 10 or not 0.5 <= self.transit_speed_ms <= 10:
            raise ValueError("speeds out of range (0.5-10 m/s)")
        far = max(math.hypot(*p) for s in self.sorties for q in s for p in q.points)
        if far > 3000:
            raise ValueError(f"a pass is {far:.0f} m from home: beyond the 3 km the job allows")
        for s in self.sorties:
            for q in s:
                for p in q.points:
                    if not self.inside_fence(p, margin=self.fence_margin_m):
                        raise ValueError("a pass leaves the fence (field polygon plus margin)")

    def inside_fence(self, p: tuple[float, float], margin: float | None = None) -> bool:
        """Inside the field polygon, or within ``margin`` metres of it (home is always allowed)."""
        m = self.fence_margin_m if margin is None else margin
        if math.hypot(*p) <= max(m, 15.0):
            return True
        if _inside(p, self.fence):
            return True
        return _dist_to_poly(p, self.fence) <= m


def _inside(p, poly) -> bool:
    x, y = p
    c = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def _dist_to_poly(p, poly) -> float:
    best = math.inf
    for a, b in zip(poly, poly[1:] + poly[:1]):
        best = min(best, _dist_seg(p, a, b))
    return best


def _dist_seg(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)
