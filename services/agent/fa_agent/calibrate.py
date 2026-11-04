"""The calibration flight: measure how this K3-A Pro answers its sticks in GPS-SPEED mode.

From a hover at >= 8 m in open ground, each axis gets step inputs at two deflections in both
directions (3 s each), with a position-hold recovery to the start point in between. For each
step the steady speed (last 1.2 s) and the time to 63 % of it are measured; a straight line
through |deflection| -> speed gives the gain and the dead band, the rise time gives the lag.
The fit is applied only if it is consistent (enough steps, sane values); otherwise the
defaults stay and the report says why.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .follower import to_body
from .profile import Axis, K3Profile

STEP_S, RECOVER_S, SETTLE_S = 3.0, 4.0, 1.2


@dataclass
class Step:
    axis: str          # "pitch" | "roll" | "yaw"
    defl: float        # signed fraction of full stick
    t0: float = 0.0
    samples: list = field(default_factory=list)    # (t, measured speed on this axis)


def plan_steps(levels=(0.3, 0.6)) -> list[Step]:
    steps = []
    for ax in ("pitch", "roll", "yaw"):
        for lv in levels:
            steps += [Step(ax, lv), Step(ax, -lv)]
    return steps


@dataclass
class AxisFit:
    gain: float
    dead: float
    tau_s: float
    n: int
    resid: float


def fit_axis(steps: list[Step]) -> AxisFit | None:
    pts, taus = [], []
    for s in steps:
        if len(s.samples) < 10:
            continue
        t = np.array([x[0] for x in s.samples]) - s.t0
        v = np.array([x[1] for x in s.samples]) * math.copysign(1, s.defl)
        ss = float(v[t >= STEP_S - SETTLE_S].mean())
        pts.append((abs(s.defl), ss))
        hit = np.where(v >= 0.632 * ss)[0] if ss > 0 else []
        if len(hit):
            taus.append(float(t[hit[0]]))
    if len({round(d, 3) for d, _ in pts}) < 2:
        return None
    d = np.array([p[0] for p in pts])
    v = np.array([p[1] for p in pts])
    a, b = np.polyfit(d, v, 1)
    if a <= 0:
        return None
    dead = max(0.0, min(0.3, -b / a))
    gain = a * (1 - dead)
    resid = float(np.sqrt(np.mean((a * d + b - v) ** 2)))
    tau = float(np.median(taus)) if taus else 0.6
    return AxisFit(float(gain), float(dead), tau, len(pts), resid)


@dataclass
class Calibrator:
    profile: K3Profile
    steps: list[Step] = field(default_factory=plan_steps)
    i: int = 0
    phase: str = "recover"
    t_phase: float = 0.0
    home: tuple | None = None
    last_heading: float | None = None
    last_t: float | None = None
    report: dict | None = None

    def command(self, now: float, fix) -> list[int] | None:
        """Channels for this tick, or None when finished (then see ``report``)."""
        if self.home is None:
            self.home = (fix.e, fix.n, fix.h)
            self.t_phase = now
        yaw_meas = 0.0
        if self.last_heading is not None and now > self.last_t:
            yaw_meas = ((fix.heading - self.last_heading + 180) % 360 - 180) / (now - self.last_t)
        self.last_heading, self.last_t = fix.heading, now
        p = self.profile
        if self.phase == "recover":
            if now - self.t_phase >= RECOVER_S:
                if self.i >= len(self.steps):
                    self.report = self._finish()
                    return None
                self.phase, self.t_phase = "step", now
                self.steps[self.i].t0 = now
            e, n, h = self.home
            ve = max(-1.5, min(1.5, 0.8 * (e - fix.e)))
            vn = max(-1.5, min(1.5, 0.8 * (n - fix.n)))
            fwd, right = to_body(ve, vn, fix.heading)
            return p.command(fwd, right, max(-1.0, min(1.0, 0.8 * (h - fix.h))), 0.0, False)
        s = self.steps[self.i]
        fwd, right = to_body(fix.v_e, fix.v_n, fix.heading)
        meas = {"pitch": fwd, "roll": right, "yaw": yaw_meas}[s.axis]
        s.samples.append((now, meas))
        us = p.command(0.0, 0.0, max(-1.0, min(1.0, 0.8 * (self.home[2] - fix.h))), 0.0, False)
        ax: Axis = getattr(p, s.axis)
        us[ax.ch] = int(round(1500 + 500 * ax.sign * s.defl))
        if now - self.t_phase >= STEP_S:
            self.i += 1
            self.phase, self.t_phase = "recover", now
        return us

    def _finish(self) -> dict:
        out = {"applied": [], "kept": {}, "axes": {}}
        for name in ("pitch", "roll", "yaw"):
            f = fit_axis([s for s in self.steps if s.axis == name])
            ax: Axis = getattr(self.profile, name)
            if f is None:
                out["kept"][name] = "not enough clean steps"
                continue
            out["axes"][name] = {"gain": round(f.gain, 3), "dead": round(f.dead, 3), "tau_s": round(f.tau_s, 2),
                                 "steps": f.n, "residual": round(f.resid, 3)}
            sane = (0.5 * ax.gain <= f.gain <= 2.0 * ax.gain) and f.resid < 0.15 * f.gain
            if not sane:
                out["kept"][name] = f"fit out of range (gain {f.gain:.2f}, residual {f.resid:.2f})"
                continue
            ax.gain, ax.dead, ax.tau_s = f.gain, f.dead, f.tau_s
            out["applied"].append(name)
        self.profile.calibrated = bool(out["applied"])
        return out
