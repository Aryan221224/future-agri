"""Path following for a speed-commanded aircraft (K3-A Pro GPS-SPEED mode, or any vehicle
that takes velocity set points).

Along the leg: the job speed, slowed to stop at the end of a pass. Across: a PID on the
cross-track error with a cap, so the aircraft is pulled back onto the line without
overshooting. Heading is held along the leg. Everything is in local metres (east, north)
and headings in degrees clockwise from north, as the RTK receiver reports them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def heading_of(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dx, dy)) % 360.0


@dataclass
class Gains:
    kp: float = 0.9          # m/s of correction per m of cross-track error
    ki: float = 0.08
    kd: float = 0.35         # on cross-track speed
    v_cross_max: float = 2.0
    i_max: float = 3.0
    k_yaw: float = 1.5       # deg/s per deg of heading error
    yaw_rate_max: float = 60.0
    kz: float = 0.8
    climb_max: float = 1.5
    decel: float = 1.2       # m/s^2 when stopping at the end of a pass
    capture_m: float = 0.25


@dataclass
class Setpoint:
    v_e: float
    v_n: float
    climb: float
    heading: float
    spray: bool
    done: bool
    xtrack: float = 0.0
    along: float = 0.0


@dataclass
class LegFollower:
    """Follows a polyline; ``stop_at_end`` makes it come to rest on the last point."""
    points: list[tuple[float, float]]
    speed: float
    height: float
    spray: bool = False
    stop_at_end: bool = True
    spray_xtrack_max: float = 1.0
    gains: Gains = field(default_factory=Gains)
    leg: int = 0
    _i: float = 0.0

    def update(self, e: float, n: float, v_e: float, v_n: float, h: float, dt: float) -> Setpoint:
        g = self.gains
        while True:
            (ax, ay), (bx, by) = self.points[self.leg], self.points[self.leg + 1]
            dx, dy = bx - ax, by - ay
            L = math.hypot(dx, dy)
            if L < 1e-6:
                if self.leg + 2 < len(self.points):
                    self.leg += 1
                    continue
                tx, ty = 0.0, 1.0
            else:
                tx, ty = dx / L, dy / L
            s = (e - ax) * tx + (n - ay) * ty
            last = self.leg + 2 >= len(self.points)
            if s >= L - g.capture_m and not last:
                self.leg += 1
                continue
            break
        nx, ny = ty, -tx                                   # right of the leg
        xt = (e - ax) * nx + (n - ay) * ny
        v_ct = v_e * nx + v_n * ny
        self._i = max(-g.i_max, min(g.i_max, self._i + xt * dt))
        corr = -(g.kp * xt + g.ki * self._i + g.kd * v_ct)
        corr = max(-g.v_cross_max, min(g.v_cross_max, corr))
        remaining = max(0.0, L - s)
        v_al = self.speed
        if last and self.stop_at_end:
            v_al = min(self.speed, math.sqrt(2 * g.decel * remaining) + 0.15 * (remaining > 0.05))
            if remaining <= 0.05:
                v_al = g.kp * remaining
        done = last and s >= L - g.capture_m and abs(xt) < max(0.5, 2 * g.capture_m) and (
            not self.stop_at_end or math.hypot(v_e, v_n) < 0.6)
        climb = max(-g.climb_max, min(g.climb_max, g.kz * (self.height - h)))
        spray = self.spray and abs(xt) <= self.spray_xtrack_max and abs(self.height - h) < 1.0 and remaining > 0.1
        return Setpoint(v_al * tx + corr * nx, v_al * ty + corr * ny, climb, heading_of(tx, ty), spray, done,
                        xt, s)


def yaw_rate(target_heading: float, heading: float, g: Gains) -> float:
    return max(-g.yaw_rate_max, min(g.yaw_rate_max, g.k_yaw * wrap180(target_heading - heading)))


def to_body(v_e: float, v_n: float, heading: float) -> tuple[float, float]:
    """World velocity -> (forward, right) for a heading clockwise from north."""
    h = math.radians(heading)
    return v_e * math.sin(h) + v_n * math.cos(h), v_e * math.cos(h) - v_n * math.sin(h)
