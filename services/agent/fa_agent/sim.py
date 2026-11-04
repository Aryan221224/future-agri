"""A simulated JIYI K3-A Pro, RTK receiver and power monitor, for flying fa-agent end to end
without hardware.

The K3-A Pro model follows its manual's GPS-SPEED mode: CH1/CH2 command lateral/forward
speed, CH3 climb rate (centre holds altitude), CH4 yaw rate, CH5 the mode, CH6 one-key
return home; it arms on a held stick combination, auto-disarms on the ground, detects the
landing and stops, and returns home by itself when the S-BUS signal stops. Its *true* stick
gains, dead bands and lag differ from the agent's defaults on purpose (the agent must cope
until it is calibrated). Wind and the K3-A Pro's own position-estimate wander enter as a
coloured disturbance on its speed loop.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .profile import Axis, K3Profile


@dataclass
class Truth:
    e: float = 0.0
    n: float = 0.0
    h: float = 0.0
    v_e: float = 0.0
    v_n: float = 0.0
    v_u: float = 0.0
    heading: float = 0.0           # deg clockwise from north
    yaw_rate: float = 0.0
    armed: bool = False
    flying: bool = False
    pump: bool = False
    rth: bool = False


@dataclass
class SimK3:
    true_profile: K3Profile = field(default_factory=lambda: _true_profile())
    wind_ms: float = 3.0
    wind_dir_deg: float = 270.0    # blowing from the west
    disturbance_ms: float = 0.12   # speed-loop wander (its own GPS estimate, gusts)
    seed: int = 0
    s: Truth = field(default_factory=Truth)
    battery_ah: float = 16.0
    used_ah: float = 0.0
    litres_sprayed: float = 0.0
    events: list = field(default_factory=list)

    def __post_init__(self):
        self.rng = np.random.default_rng(self.seed)
        self._arm_held = 0.0
        self._ground_low = 0.0
        self._no_signal = 0.0
        self._last = None
        self._dist = np.zeros(2)
        self._t = 0.0

    # --------------------------------------------------------------- inputs
    def _axis(self, ax: Axis, us: list[int]) -> float:
        return ax.from_us(us[ax.ch])

    def _arm_pose(self, us: list[int]) -> bool:
        for ms, chs in self.true_profile.arm_sequence:
            if all(abs(us[c] - v) <= 100 for c, v in chs.items()):
                return True
        return False

    def step(self, us: list[int] | None, dt: float) -> None:
        """Advance ``dt`` seconds; ``us`` is the S-BUS frame received this step (None = no signal)."""
        self._t += dt
        p, s = self.true_profile, self.s
        if us is None:
            self._no_signal += dt
            us = self._last
        else:
            self._no_signal = 0.0
            self._last = us
        signal_lost = self._no_signal > 1.0 or us is None
        if us is None:
            us = [1500] * 16
            us[p.throttle.ch] = 1000
        gps_mode = abs(us[p.mode_ch] - p.mode_gps_us) <= 100
        want_rth = (us[p.rth_ch] >= 1700 or signal_lost) and (s.armed and s.flying)
        if want_rth and not s.rth:
            self.events.append((round(self._t, 2), "rth", "signal lost" if signal_lost else "CH6"))
        s.rth = want_rth or (s.rth and s.flying)

        # arming / disarming on the ground
        if not s.armed:
            if self._arm_pose(us) and gps_mode:
                self._arm_held += dt
                if self._arm_held >= 2.0:
                    s.armed = True
                    self._ground_low = 0.0
                    self.events.append((round(self._t, 2), "armed", ""))
            else:
                self._arm_held = 0.0
            self._integrate(0.0, 0.0, 0.0, 0.0, dt, ground=True)
            return
        if not s.flying:
            thr = self._axis(p.throttle, us)
            if thr > 0.3:
                s.flying = True
                self.events.append((round(self._t, 2), "takeoff", ""))
            else:
                self._ground_low += dt
                if self._ground_low >= 3.0 and not self._arm_pose(us):
                    s.armed = False
                    self.events.append((round(self._t, 2), "disarmed", "auto"))
                self._integrate(0.0, 0.0, 0.0, 0.0, dt, ground=True)
                return

        # flying
        if s.rth:
            fwd_e, fwd_n = -s.e, -s.n
            d = math.hypot(fwd_e, fwd_n)
            v = min(5.0, 0.8 * d)
            ve, vn = (v * fwd_e / d, v * fwd_n / d) if d > 1e-3 else (0.0, 0.0)
            climb = (1.5 if s.h < 10.0 and d > 3.0 else (-0.8 if d <= 3.0 else 0.0))
            yaw = 0.0
        elif not gps_mode:
            ve, vn, climb, yaw = s.v_e, s.v_n, 0.0, 0.0           # attitude mode: drifts (never used)
        else:
            fwd, right = self._axis(p.pitch, us), self._axis(p.roll, us)
            climb, yaw = self._axis(p.throttle, us), self._axis(p.yaw, us)
            h = math.radians(s.heading)
            ve = fwd * math.sin(h) + right * math.cos(h)
            vn = fwd * math.cos(h) - right * math.sin(h)
        s.pump = (us[p.pump_ch] >= 1600) and not s.rth
        self._integrate(ve, vn, climb, yaw, dt, ground=False)
        if s.h <= 0.0 and s.v_u <= 0.05:
            s.h = 0.0
            thr = self._axis(p.throttle, us)
            if thr < 0.0 or s.rth:
                self._ground_low += dt
                if self._ground_low >= 1.0:
                    s.flying = s.armed = s.rth = False
                    s.v_e = s.v_n = s.v_u = 0.0
                    self.events.append((round(self._t, 2), "landed", "disarmed"))
        else:
            self._ground_low = 0.0

    def _integrate(self, ve, vn, climb, yaw, dt, ground: bool) -> None:
        s, p = self.s, self.true_profile
        if ground:
            s.v_e = s.v_n = s.v_u = 0.0
            s.pump = False
            self._current(dt)
            return
        tau = p.pitch.tau_s
        a = 1.0 - math.exp(-dt / tau)
        theta = 0.98 ** (dt / 0.01)                               # disturbance correlation ~0.5 s
        self._dist = theta * self._dist + math.sqrt(1 - theta ** 2) * self.disturbance_ms * self.rng.normal(size=2)
        w = math.radians(self.wind_dir_deg)
        bias = 0.04 * self.wind_ms                                  # residual wind error of its speed loop
        be, bn = -bias * math.sin(w), -bias * math.cos(w)
        s.v_e += a * (ve + be + self._dist[0] - s.v_e)
        s.v_n += a * (vn + bn + self._dist[1] - s.v_n)
        s.v_u += (1.0 - math.exp(-dt / p.throttle.tau_s)) * (climb - s.v_u)
        s.yaw_rate += (1.0 - math.exp(-dt / p.yaw.tau_s)) * (yaw - s.yaw_rate)
        s.heading = (s.heading + s.yaw_rate * dt) % 360.0
        s.e += s.v_e * dt
        s.n += s.v_n * dt
        s.h = max(0.0, s.h + s.v_u * dt)
        if s.pump:
            self.litres_sprayed += 4.0 / 60.0 * dt
        self._current(dt)

    def _current(self, dt: float) -> None:
        s = self.s
        amps = 0.3 if not s.armed else (2.5 if not s.flying else 55.0 + 4.0 * math.hypot(s.v_e, s.v_n))
        self.amps = amps
        self.used_ah += amps * dt / 3600.0

    def voltage(self) -> float:
        soc = max(0.0, 1.0 - self.used_ah / self.battery_ah)
        cell = 3.5 + 0.7 * soc - 0.15 * (soc < 0.15) * (0.15 - soc) / 0.15
        return 12 * cell - 0.02 * getattr(self, "amps", 0.0)


def _true_profile() -> K3Profile:
    """What the real aircraft does (unknown to the agent until calibrated)."""
    p = K3Profile()
    p.pitch = Axis(1, 5.6, dead=0.07, tau_s=0.75)
    p.roll = Axis(0, 4.6, dead=0.08, tau_s=0.75)
    p.throttle = Axis(2, 1.8, dead=0.12, tau_s=0.5)
    p.yaw = Axis(3, 80.0, dead=0.06, tau_s=0.3)
    return p


@dataclass
class Fix:
    t: float
    e: float
    n: float
    h: float
    v_e: float
    v_n: float
    v_u: float
    heading: float
    heading_ok: bool
    kind: str            # "none" | "3d" | "float" | "fixed"
    sats: int = 24


@dataclass
class SimGnss:
    """An RTK moving-base pair on the sim aircraft: 10 Hz, noise by solution type."""
    k3: SimK3
    seed: int = 1
    kind: str = "fixed"
    heading_ok: bool = True
    period_s: float = 0.1

    def __post_init__(self):
        self.rng = np.random.default_rng(self.seed)
        self._next = 0.0
        self.last: Fix | None = None

    def poll(self, t: float) -> Fix | None:
        if t + 1e-9 < self._next:
            return self.last
        self._next = t + self.period_s
        if self.kind == "none":
            self.last = None
            return None
        sd = {"fixed": 0.012, "float": 0.25, "3d": 1.2}[self.kind]
        s = self.k3.s
        r = self.rng.normal(size=6)
        self.last = Fix(t, s.e + sd * r[0], s.n + sd * r[1], s.h + 1.5 * sd * r[2], s.v_e + 0.03 * r[3],
                        s.v_n + 0.03 * r[4], s.v_u, (s.heading + 0.25 * r[5]) % 360.0, self.heading_ok, self.kind)
        return self.last


@dataclass
class Power:
    voltage: float
    current: float
    t: float
