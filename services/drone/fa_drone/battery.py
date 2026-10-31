"""12S LiPo pack as an equivalent circuit: OCV(SOC) + R0 + one RC pair, with
temperature derating, cycle ageing and the low-voltage return threshold.

Parameter values are typical for agricultural LiPo packs and are labelled
assumptions; fit R0, R1, C1 and the OCV curve from charger/BMS and flight-
controller voltage logs before trusting endurance to the minute.
"""
from __future__ import annotations

import math

import numpy as np

from .spec import DroneSpec

#: Per-cell open-circuit voltage vs state of charge (typical LiPo).
SOC_PTS = np.array([0.0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00])
OCV_PTS = np.array([3.27, 3.61, 3.69, 3.73, 3.77, 3.79, 3.82, 3.87, 3.92, 3.98, 4.06, 4.20])


class BatteryCollapse(RuntimeError):
    """Requested power exceeds what the pack can deliver at this state."""


class Battery:
    def __init__(self, spec: DroneSpec, temp_c: float = 25.0, cycles: int = 0, soc: float = 1.0,
                 r0_cell_ohm: float = 0.0018, r1_cell_ohm: float = 0.0010, tau_s: float = 30.0,
                 rth_cell_v: float | None = None):
        if rth_cell_v is None:
            rth_cell_v = calibrated_rth_cell_v(spec, r0_cell_ohm, r1_cell_ohm, tau_s)
        b = spec.battery
        self.cells = b.cells
        cold = max(0.0, 25.0 - temp_c)
        self.capacity_ah = b.capacity_ah * (1 - 0.006 * cold) * max(0.5, 1 - 0.0004 * cycles)
        self.r0 = r0_cell_ohm * b.cells * math.exp(0.03 * cold) * (1 + 0.001 * cycles)
        self.r1 = r1_cell_ohm * b.cells * math.exp(0.03 * cold)
        self.c1 = tau_s / self.r1
        self.rth_v = rth_cell_v * b.cells
        self.soc = float(soc)
        self.v1 = 0.0
        self.last_v = self.ocv()
        self.wh_drawn = 0.0

    def ocv(self, soc: float | None = None) -> float:
        s = self.soc if soc is None else soc
        return float(np.interp(s, SOC_PTS, OCV_PTS)) * self.cells

    def draw(self, power_w: float, dt_s: float) -> float:
        """Deliver ``power_w`` for ``dt_s``; return the terminal voltage."""
        e = self.ocv() - self.v1
        disc = e * e - 4 * self.r0 * power_w
        if disc < 0:
            raise BatteryCollapse(f"cannot deliver {power_w:.0f} W at SOC {self.soc:.2f}")
        i = (e - math.sqrt(disc)) / (2 * self.r0)
        self.v1 += (i / self.c1 - self.v1 / (self.r1 * self.c1)) * dt_s
        self.soc = max(0.0, self.soc - i * dt_s / (3600.0 * self.capacity_ah))
        self.last_v = e - i * self.r0
        self.wh_drawn += power_w * dt_s / 3600.0
        return self.last_v

    def low_voltage(self) -> bool:
        return self.last_v <= self.rth_v or self.soc <= 0.02

    def usable_wh_left(self, load_w: float = 3000.0) -> float:
        """Energy available before the low-voltage threshold at a steady load."""
        i = load_w / (self.cells * 3.8)
        sag = i * (self.r0 + self.r1)
        floor_ocv = (self.rth_v + sag) / self.cells
        soc_floor = float(np.interp(floor_ocv, OCV_PTS, SOC_PTS))
        if self.soc <= soc_floor:
            return 0.0
        s = np.linspace(soc_floor, self.soc, 50)
        v = np.interp(s, SOC_PTS, OCV_PTS) * self.cells
        return float(np.trapezoid(v, s) * self.capacity_ah)


_RTH_CACHE: dict = {}


def calibrated_rth_cell_v(spec: DroneSpec, r0_cell_ohm: float = 0.0018,
                          r1_cell_ohm: float = 0.0010, tau_s: float = 30.0) -> float:
    """Return-home cell voltage (under load) at which a fresh pack at 25 C has
    delivered the spec sheet's usable energy at the heavy hover point.

    The sheet's 12 min @ 24 kg implies how deep the pack is flown; this makes the
    battery model and the power model agree on it instead of hard-coding a
    threshold that contradicts the sheet."""
    key = (spec.name, r0_cell_ohm, r1_cell_ohm, tau_s)
    if key in _RTH_CACHE:
        return _RTH_CACHE[key]
    from .power import PowerModel
    pm = PowerModel.calibrate(spec)
    m, _ = max(spec.hover_points)
    load = pm.hover_w(m)
    target = spec.battery.nominal_wh * spec.battery.usable_frac
    lo, hi = 3.0, 3.9
    for _ in range(30):
        mid = 0.5 * (lo + hi)
        b = Battery(spec, r0_cell_ohm=r0_cell_ohm, r1_cell_ohm=r1_cell_ohm, tau_s=tau_s,
                    rth_cell_v=mid)
        try:
            while not b.low_voltage():
                b.draw(load, 2.0)
        except BatteryCollapse:
            pass
        if b.wh_drawn > target:
            lo = mid           # threshold too low -> raise it
        else:
            hi = mid
    _RTH_CACHE[key] = 0.5 * (lo + hi)
    return _RTH_CACHE[key]
