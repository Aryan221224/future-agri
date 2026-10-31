"""Spray-window rules for drone pesticide application in India.

India's drone-spraying SOPs (MoA&FW interim SOP 2021; crop-specific SOPs
2023) restrict when a drone may spray. The planner only proposes a spray on a
day that passes these checks, and the executor re-checks on the day.

The thresholds below are the ones used in the 30 Oct 2026 audit: no rain,
wind under 3 m/s, temperature at or below 35 C. **Verify them against the SOP
text and the product label before any field spraying** -- labels often add
their own wind, temperature and re-entry limits, and the label wins.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class SprayWindow:
    max_rain_mm: float = 1.0
    max_wind_ms: float = 3.0
    max_tmax_c: float = 35.0
    source: str = "audit 30 Oct 2026 reading of the 2021/2023 SOPs; verify against SOP + label"

    def ok(self, rec: dict) -> bool:
        return (rec["RAIN"] < self.max_rain_mm and rec["WIND"] < self.max_wind_ms
                and rec["TMAX"] <= self.max_tmax_c)

    def reasons(self, rec: dict) -> list[str]:
        out = []
        if rec["RAIN"] >= self.max_rain_mm:
            out.append(f"rain {rec['RAIN']:.1f} mm")
        if rec["WIND"] >= self.max_wind_ms:
            out.append(f"wind {rec['WIND']:.1f} m/s")
        if rec["TMAX"] > self.max_tmax_c:
            out.append(f"Tmax {rec['TMAX']:.1f} C")
        return out


DEFAULT_WINDOW = SprayWindow()


def first_window(records: list[dict], start: dt.date, days: int = 7,
                 window: SprayWindow = DEFAULT_WINDOW) -> dt.date | None:
    """First sprayable day in ``[start, start+days)`` of one weather sequence."""
    end = start + dt.timedelta(days=days)
    for r in records:
        if start <= r["DAY"] < end and window.ok(r):
            return r["DAY"]
    return None


def consensus_window(members: list[list[dict]], start: dt.date, days: int = 7,
                     min_fraction: float = 0.5,
                     window: SprayWindow = DEFAULT_WINDOW) -> dt.date | None:
    """First day in the week on which at least ``min_fraction`` of forecast members are sprayable."""
    for k in range(days):
        d = start + dt.timedelta(days=k)
        ok = 0
        for m in members:
            rec = next((r for r in m if r["DAY"] == d), None)
            ok += bool(rec is not None and window.ok(rec))
        if members and ok / len(members) >= min_fraction:
            return d
    return None
