"""How the K3-A Pro reads its sticks (GPS-SPEED mode), as a calibrated profile.

speed = gain x (|d| - dead) / (1 - dead) x sign(d), d = (us - 1500) / 500 per axis.
Defaults come from the K3-A Pro's speed gear and a typical dead band; the calibration flight
(fa_agent.calibrate) replaces them with measured values. Anything marked ``bench: verify``
in data/k3a_pro.yaml is confirmed by the props-off bench test before the first flight.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

DATA = Path(__file__).parent / "data"


@dataclass
class Axis:
    ch: int                 # 0-based S-BUS channel
    gain: float             # speed (m/s, or deg/s for yaw) at full deflection
    dead: float = 0.05      # dead band, fraction of full deflection
    sign: int = 1           # +1: higher pulse = forward / right / up / clockwise
    tau_s: float = 0.6      # first-order response time (calibration measures it)

    def to_us(self, v: float) -> int:
        if abs(v) < 1e-6:
            return 1500
        frac = min(1.0, abs(v) / self.gain)
        d = self.dead + frac * (1.0 - self.dead)
        return int(round(1500 + 500 * self.sign * math.copysign(d, v)))

    def from_us(self, us: float) -> float:
        d = self.sign * (us - 1500) / 500.0
        a = abs(d)
        if a <= self.dead:
            return 0.0
        return math.copysign(self.gain * min(1.0, (a - self.dead) / (1.0 - self.dead)), d)


@dataclass
class K3Profile:
    roll: Axis = field(default_factory=lambda: Axis(0, 5.0))       # lateral speed (right +)
    pitch: Axis = field(default_factory=lambda: Axis(1, 5.0))      # forward speed
    throttle: Axis = field(default_factory=lambda: Axis(2, 2.0, dead=0.10))   # climb rate
    yaw: Axis = field(default_factory=lambda: Axis(3, 90.0, dead=0.05))       # deg/s clockwise
    mode_ch: int = 4
    mode_gps_us: int = 1500
    rth_ch: int = 5
    rth_on_us: int = 2000
    rth_off_us: int = 1000
    pump_ch: int = 6
    pump_on_us: int = 2000
    pump_off_us: int = 1000
    auto_ch: int = 7
    # arming: a stick combination held; [(ms, {ch: us})]
    arm_sequence: list = field(default_factory=lambda: [[2500, {2: 1000, 3: 1000, 0: 2000, 1: 1000}]])
    idle_current_a: float = 2.0      # armed on the ground (power monitor): motors spinning
    calibrated: bool = False

    @classmethod
    def load(cls, path: str | Path | None = None) -> "K3Profile":
        d = yaml.safe_load(Path(path or DATA / "k3a_pro.yaml").read_text())
        axes = {k: Axis(**d.pop(k)) for k in ("roll", "pitch", "throttle", "yaw") if k in d}
        d["arm_sequence"] = [[int(ms), {int(c): int(v) for c, v in chs.items()}] for ms, chs in
                             d.get("arm_sequence", [])] or cls().arm_sequence
        return cls(**axes, **{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def save(self, path: str | Path) -> None:
        d = asdict(self)
        d["arm_sequence"] = [[ms, {int(c): int(v) for c, v in chs.items()}] for ms, chs in self.arm_sequence]
        Path(path).write_text(yaml.safe_dump(d, sort_keys=False))

    def neutral(self) -> list[int]:
        us = [1500] * 16
        us[self.mode_ch] = self.mode_gps_us
        us[self.rth_ch] = self.rth_off_us
        us[self.pump_ch] = self.pump_off_us
        us[self.auto_ch] = 2000
        return us

    def command(self, fwd: float, right: float, climb: float, yaw_rate: float, pump: bool,
                rth: bool = False) -> list[int]:
        us = self.neutral()
        us[self.pitch.ch] = self.pitch.to_us(fwd)
        us[self.roll.ch] = self.roll.to_us(right)
        us[self.throttle.ch] = self.throttle.to_us(climb)
        us[self.yaw.ch] = self.yaw.to_us(yaw_rate)
        us[self.pump_ch] = self.pump_on_us if pump else self.pump_off_us
        us[self.rth_ch] = self.rth_on_us if rth else self.rth_off_us
        return us
