"""A kinematic multirotor simulator with no external dependencies.

Why this exists alongside PX4 SITL
----------------------------------
The real simulation backbone is PX4 v1.17 SITL with Gazebo Harmonic on Ubuntu
24.04, and ``fa_sim.px4`` drives it over MAVSDK. But that stack is a multi-
gigabyte install with a specific OS and Gazebo version pairing, and it cannot
run in CI or on a laptop that has not been set up for it.

So the mission executor is written against an interface, and this is the
implementation that always works. It integrates position at commanded speed,
applies the same energy model the router planned with, triggers the camera at
the planned distance interval, and reports the same telemetry shape. That means
the *whole* closed loop -- coverage plan, energy-feasible sorties, frame
capture, edge inference, conformal gating, assimilation, replanning -- is
exercised end to end with `make demo`, on any machine, before any hardware
exists.

What it deliberately does not model: aerodynamics, wind, control-loop dynamics,
GPS error, sensor noise. It is a scheduling and energy simulator, not a flight
dynamics simulator. Anything that depends on those must be tested in SITL, and
the code says so rather than letting a kinematic result masquerade as a flight
test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class TelemetrySample:
    t_s: float
    x: float
    y: float
    z: float
    speed_ms: float
    energy_used_wh: float
    battery_frac: float
    mode: str


@dataclass
class CaptureEvent:
    t_s: float
    x: float
    y: float
    z: float
    swath_index: int
    frame_id: str


@dataclass
class FlightResult:
    telemetry: list[TelemetrySample]
    captures: list[CaptureEvent]
    duration_s: float
    energy_wh: float
    distance_m: float
    completed: bool
    aborted_reason: str = ""

    @property
    def n_frames(self) -> int:
        return len(self.captures)


class KinematicDrone:
    """Constant-speed point-mass with the routing energy model."""

    def __init__(self, drone_model, dt: float = 0.5, home=(0.0, 0.0),
                 altitude_m: float = 60.0, trigger_distance_m: float = 15.0,
                 reserve_abort_frac: float = 0.05):
        self.d = drone_model
        self.dt = dt
        self.home = home
        self.altitude = altitude_m
        self.trigger_distance = trigger_distance_m
        self.reserve_abort_frac = reserve_abort_frac

    def fly_sortie(self, sortie, start_time: datetime | None = None) -> FlightResult:
        d = self.d
        t = 0.0
        energy = d.takeoff_landing_wh
        dist = 0.0
        pos = self.home
        tel: list[TelemetrySample] = []
        caps: list[CaptureEvent] = []
        since_trigger = 0.0
        budget = d.usable_energy_wh

        def sample(mode: str, speed: float):
            tel.append(TelemetrySample(
                round(t, 2), pos[0], pos[1], self.altitude, speed,
                round(energy, 3), max(0.0, 1.0 - energy / budget), mode))

        sample("takeoff", 0.0)
        for sw in sortie.swaths:
            # transit to the swath entry
            leg = math.dist(pos, sw.start)
            steps = max(1, int(math.ceil(leg / max(d.cruise_speed_ms * self.dt, 1e-9))))
            for i in range(steps):
                f = (i + 1) / steps
                pos = (sortie.swaths and (
                    tel[-1].x + (sw.start[0] - tel[-1].x) * (1.0 / (steps - i)),
                    tel[-1].y + (sw.start[1] - tel[-1].y) * (1.0 / (steps - i))))
                step_m = leg / steps
                dist += step_m
                energy += d.transit_wh(step_m)
                t += step_m / d.cruise_speed_ms
                sample("transit", d.cruise_speed_ms)
                if energy > budget * (1 - self.reserve_abort_frac):
                    return FlightResult(tel, caps, t, energy, dist, False,
                                        "battery reserve reached in transit")
            pos = sw.start
            energy += d.turn_energy_wh

            # fly the swath, triggering the camera by distance
            length = sw.length_m
            steps = max(1, int(math.ceil(length / max(d.survey_speed_ms * self.dt, 1e-9))))
            ux = (sw.end[0] - sw.start[0]) / max(length, 1e-9)
            uy = (sw.end[1] - sw.start[1]) / max(length, 1e-9)
            for i in range(steps):
                step_m = length / steps
                pos = (pos[0] + ux * step_m, pos[1] + uy * step_m)
                dist += step_m
                energy += d.survey_wh(step_m)
                t += step_m / d.survey_speed_ms
                since_trigger += step_m
                if since_trigger >= self.trigger_distance:
                    since_trigger = 0.0
                    caps.append(CaptureEvent(round(t, 2), pos[0], pos[1], self.altitude,
                                             sw.index, f"s{sw.index}_f{len(caps):05d}"))
                sample("survey", d.survey_speed_ms)
                if energy > budget * (1 - self.reserve_abort_frac):
                    return FlightResult(tel, caps, t, energy, dist, False,
                                        "battery reserve reached mid-swath")
            pos = sw.end

        leg = math.dist(pos, self.home)
        dist += leg
        energy += d.transit_wh(leg)
        t += leg / d.cruise_speed_ms
        pos = self.home
        sample("rtl", d.cruise_speed_ms)
        sample("landed", 0.0)
        return FlightResult(tel, caps, t, energy, dist, True)

    def fly_mission(self, plan) -> list[FlightResult]:
        return [self.fly_sortie(s) for s in plan.sorties]
