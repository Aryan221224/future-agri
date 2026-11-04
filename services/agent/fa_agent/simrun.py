"""Fly a job end to end in simulation: virtual pilot -> the bridge's real C core -> a
simulated K3-A Pro, with a simulated RTK pair and power monitor.

    python -m fa_agent sim --job job.json        # or: run(job) from Python / tests

Timing as on the aircraft: physics 100 Hz, S-BUS frames every 14 ms, the pilot's receiver
every 14 ms, the agent at 20 Hz with a 200 ms frame time-to-live. Faults can be injected at
given times (``faults``: list of (t_s, name, value)).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .bridgelink import CoreBridge
from .pilot import Pilot, PilotConfig
from .profile import K3Profile
from .route import Job
from .sim import Power, SimGnss, SimK3


@dataclass
class SimResult:
    state: str
    events: list
    k3_events: list
    xtrack_rms: float
    xtrack_p95: float
    xtrack_max: float
    spray_s: float
    litres: float
    duration_s: float
    track: list = field(default_factory=list)
    bridge_sources: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"state": self.state, "xtrack_rms_m": round(self.xtrack_rms, 3),
                "xtrack_p95_m": round(self.xtrack_p95, 3), "xtrack_max_m": round(self.xtrack_max, 3),
                "spray_s": round(self.spray_s, 1), "litres": round(self.litres, 2),
                "duration_s": round(self.duration_s, 1), "bridge_sources": self.bridge_sources,
                "events": [(e.t, e.state, e.reason) for e in self.events]}


def run(job: Job, max_s: float = 1800.0, profile: K3Profile | None = None, k3: SimK3 | None = None,
        faults: list | None = None, auto_switch: bool = True, swap_battery_after_s: float = 20.0,
        seed: int = 0, pilot_cfg: PilotConfig | None = None, agent_alive: bool = True,
        calibrate: bool = False, pilot: Pilot | None = None) -> SimResult:
    profile = profile or K3Profile.load()
    k3 = k3 or SimK3(seed=seed)
    gnss = SimGnss(k3, seed=seed + 1)
    bridge = CoreBridge()
    pilot = pilot or Pilot(profile, pilot_cfg or PilotConfig())
    faults = sorted(faults or [], key=lambda f: f[0])
    dt, t, next_frame, next_agent = 0.01, 0.0, 0.0, 0.0
    out_us, landed_at = None, None
    xt, spray_s, track, sources = [], 0.0, [], {}
    pilot_us = [1500] * 16
    pilot_us[profile.throttle.ch] = 1000
    pilot_us[profile.auto_ch] = 2000 if auto_switch else 1000
    pilot_rx = True
    if calibrate:
        pilot.start_calibration(job)
    else:
        pilot.start(job)
    while t < max_s:
        while faults and faults[0][0] <= t:
            _, name, val = faults.pop(0)
            if name == "rtk":
                gnss.kind = val
            elif name == "heading":
                gnss.heading_ok = bool(val)
            elif name == "auto_switch":
                pilot_us[profile.auto_ch] = 2000 if val else 1000
                if not val and k3.s.flying:
                    pilot_us[profile.throttle.ch] = 1500    # a pilot taking over holds the hover
            elif name == "pilot_throttle":
                pilot_us[profile.throttle.ch] = int(val)
            elif name == "pilot_rx":
                pilot_rx = bool(val)
            elif name == "agent":
                agent_alive = bool(val)
            elif name == "abort":
                pilot.abort()
            elif name == "pause":
                pilot.pause()
            elif name == "resume":
                pilot.resume()
            elif name == "wind":
                k3.wind_ms = float(val)
            elif name == "battery_used_ah":
                k3.used_ah = float(val)
        ms = int(round(t * 1000))
        if t + 1e-9 >= next_frame:
            next_frame += 0.014
            if pilot_rx:
                bridge.pilot(pilot_us, ms)
            src, out_us = bridge.output(ms)
            sources[src] = sources.get(src, 0) + 1
        if t + 1e-9 >= next_agent:
            next_agent += 1.0 / pilot.cfg.tick_hz
            fix = gnss.poll(t)
            power = Power(k3.voltage(), getattr(k3, "amps", 0.0), t)
            status = bridge.status(ms)
            status.at = t
            if agent_alive:
                us = pilot.tick(t, fix, power, status)
                if us is not None:
                    bridge.companion(us, 200, ms)
            if pilot.state == "PASS" and pilot.xtrack is not None and pilot.spraying:
                xt.append(_true_xtrack(pilot, k3))
                spray_s += 1.0 / pilot.cfg.tick_hz
            track.append((round(t, 2), round(k3.s.e, 3), round(k3.s.n, 3), round(k3.s.h, 3), pilot.state,
                          bool(k3.s.pump)))
            if pilot.state == "LANDED":
                landed_at = landed_at if landed_at is not None else t
                if t - landed_at >= swap_battery_after_s:
                    k3.used_ah = 0.0
                    pilot.battery_swapped()
                    landed_at = None
            if pilot.state in ("DONE", "FAILED", "IDLE") and t > 1.0 and not k3.s.flying:
                break
        k3.step(out_us, dt)
        t += dt
    a = np.abs(np.asarray(xt)) if xt else np.zeros(1)
    res = SimResult(pilot.state, pilot.events, k3.events, float(np.sqrt((a ** 2).mean())),
                     float(np.percentile(a, 95)), float(a.max()), spray_s, k3.litres_sprayed, t, track, sources)
    res.pilot = pilot
    return res


def _true_xtrack(pilot: Pilot, k3: SimK3) -> float:
    """Cross-track error of the *true* aircraft position from the leg being flown."""
    f = pilot._follower
    (ax, ay), (bx, by) = f.points[f.leg], f.points[f.leg + 1]
    L = math.hypot(bx - ax, by - ay) or 1.0
    tx, ty = (bx - ax) / L, (by - ay) / L
    return (k3.s.e - ax) * ty - (k3.s.n - ay) * tx


def demo_job(n_passes: int = 4, length_m: float = 60.0, spacing_m: float = 4.0, sorties: int = 1,
             offset_e: float = 10.0, offset_n: float = 10.0) -> Job:
    """A rectangular field north-east of home with boustrophedon passes."""
    from .route import Local, Pass
    loc = Local(28.47, 77.50)
    passes = []
    for i in range(n_passes):
        e = offset_e + i * spacing_m
        a, b = (e, offset_n), (e, offset_n + length_m)
        passes.append(Pass([a, b] if i % 2 == 0 else [b, a]))
    per = math.ceil(n_passes / sorties)
    fence = [(offset_e - 2, offset_n - 2), (offset_e + n_passes * spacing_m + 2, offset_n - 2),
             (offset_e + n_passes * spacing_m + 2, offset_n + length_m + 2), (offset_e - 2, offset_n + length_m + 2)]
    job = Job("demo", loc, fence, [passes[i:i + per] for i in range(0, n_passes, per)])
    job.validate()
    return job
