"""Fly a spray job on an ArduPilot Copter, sortie by sortie.

    preflight -> upload fence + mission (read back and compared) -> GUIDED -> arm -> take off
    -> AUTO (the mission sprays the passes and ends with RTL) -> landed and disarmed
    -> wait for the battery swap and refill -> next sortie ... -> done

ArduPilot's own failsafes stay in charge in the air (RC, GCS heartbeat, battery, EKF,
fence); this runner watches for them, records them and never fights them: if the aircraft
leaves AUTO on its own (RTL, LAND, the pilot's mode switch), the sortie is marked
interrupted and the runner waits for it to land. ``abort()`` sends RTL.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from pymavlink import mavutil

from .mission import fence_items, sortie_items
from .vehicle import Vehicle, VehicleError

M = mavutil.mavlink
PARAMS = Path(__file__).parent / "params" / "t1_10l_quad.param"
# EKF flags that must be good to fly a mission
EKF_NEEDED = (M.EKF_ATTITUDE | M.EKF_VELOCITY_HORIZ | M.EKF_POS_HORIZ_ABS | M.EKF_POS_VERT_ABS)


def load_param_file(path: str | Path = PARAMS, keys: tuple[str, ...] | None = None) -> dict[str, float]:
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.replace(",", " ").split()
        if len(parts) >= 2:
            out[parts[0]] = float(parts[1])
    return {k: v for k, v in out.items() if keys is None or k in keys}


# the subset checked before every job: a wrong value here makes a flight unsafe or wrong
SAFETY_PARAMS = ("FENCE_ENABLE", "FENCE_TYPE", "FENCE_ACTION", "FS_THR_ENABLE", "FS_GCS_ENABLE",
                 "BATT_FS_LOW_ACT", "BATT_FS_CRT_ACT", "FS_EKF_ACTION", "SPRAY_ENABLE", "RTL_ALT")


@dataclass
class PreflightReport:
    ok: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def preflight(v: Vehicle, min_cell_v: float = 3.85, cells: int = 12, need_rtk: bool = False,
              expected: dict | None = None) -> PreflightReport:
    s, f, w = v.state, [], []
    if not v.link_ok():
        f.append("no heartbeat from the aircraft for 3 s")
    if s.armed:
        f.append("already armed")
    if s.gps_fix < (5 if need_rtk else 3):
        f.append(f"GPS fix type {s.gps_fix} (need {'RTK float/fixed (5/6)' if need_rtk else '3D (3)'})")
    if s.sats < 10:
        f.append(f"{s.sats} satellites (need 10)")
    if s.hdop > 1.5:
        f.append(f"HDOP {s.hdop:.1f} (need 1.5 or better)")
    if (s.ekf_flags & EKF_NEEDED) != EKF_NEEDED:
        f.append("EKF not ready (attitude, velocity, horizontal and vertical position)")
    if s.voltage is None:
        f.append("no battery voltage")
    elif s.voltage / cells < min_cell_v:
        f.append(f"battery {s.voltage / cells:.2f} V/cell (needs {min_cell_v:.2f})")
    if s.rc_ok is False:
        w.append("no RC receiver signal: the pilot cannot take over")
    if expected:
        f += [f"parameter {x}" for x in v.check_params(expected)]
    recent = time.time() - 35.0                      # ArduPilot repeats PreArm lines every 30 s
    pre = sorted({t for at, _, t in list(s.texts) if t.startswith("PreArm") and at >= recent})
    if pre:
        w += pre                                     # arm() reports them if they still hold
    return PreflightReport(not f, f, w)


@dataclass
class Event:
    t: float
    state: str
    reason: str


class JobRunner:
    def __init__(self, vehicle: Vehicle, job, check_params: bool = True, need_rtk: bool = False,
                 min_cell_v: float = 3.85, cells: int = 12, on_event=None):
        self.v, self.job = vehicle, job
        self.expected = load_param_file(keys=SAFETY_PARAMS) if check_params else None
        self.need_rtk, self.min_cell_v, self.cells = need_rtk, min_cell_v, cells
        self.on_event = on_event
        self.state = "IDLE"
        self.sortie = 0
        self.events: list[Event] = []
        self.preflight: PreflightReport | None = None
        self._swapped = threading.Event()
        self._abort = threading.Event()

    def log(self, state: str, reason: str) -> None:
        self.state = state
        e = Event(round(time.time(), 1), state, reason)
        self.events.append(e)
        if self.on_event:
            try:
                self.on_event(e)
            except Exception:  # noqa: BLE001
                pass

    def battery_swapped(self) -> None:
        self._swapped.set()

    def abort(self) -> None:
        self._abort.set()
        try:
            self.v.set_mode("RTL")
            self.log("RTL", "operator abort: returning home")
        except VehicleError as e:
            self.log(self.state, f"abort: RTL not accepted ({e})")

    def run(self, preflight_timeout_s: float = 120.0, swap_timeout_s: float = 3600.0) -> str:
        try:
            for k in range(len(self.job.sorties)):
                self.sortie = k
                if self._abort.is_set():
                    break
                self._fly_sortie(k, preflight_timeout_s)
                if self._abort.is_set() or k + 1 >= len(self.job.sorties):
                    break
                self.log("WAIT_SWAP", f"sortie {k + 1} done: swap the battery, refill, then confirm")
                if not self._swapped.wait(swap_timeout_s):
                    self.log("STOPPED", "no battery swap confirmed")
                    return self.state
                self._swapped.clear()
            self.log("DONE" if not self._abort.is_set() else "ABORTED", "job finished")
        except VehicleError as e:
            self.log("FAILED", str(e))
        return self.state

    def _fly_sortie(self, k: int, preflight_timeout_s: float) -> None:
        v, job = self.v, self.job
        self.log("PREFLIGHT", f"sortie {k + 1} of {len(job.sorties)}")
        end = time.monotonic() + preflight_timeout_s
        while True:
            rep = preflight(v, self.min_cell_v, self.cells, self.need_rtk, self.expected)
            self.preflight = rep
            if rep.ok:
                break
            if time.monotonic() > end:
                raise VehicleError("preflight failed: " + "; ".join(rep.failures))
            time.sleep(1.0)
        self.log("UPLOAD", "fence and mission")
        v.upload(fence_items(job), M.MAV_MISSION_TYPE_FENCE)
        items = sortie_items(job, k)
        v.upload(items, M.MAV_MISSION_TYPE_MISSION)
        v.set_mode("GUIDED")
        self.log("ARMING", "")
        v.arm()
        self.log("TAKEOFF", f"to {job.transit_height_m:.0f} m")
        v.takeoff(job.transit_height_m)
        self.log("AUTO", f"mission: {len(items)} items")
        v.start_mission()
        self._watch(len(items))

    def _watch(self, n_items: int, timeout_s: float = 3 * 3600) -> None:
        v, end, interrupted = self.v, time.monotonic() + timeout_s, None
        last_seq = -1
        while time.monotonic() < end:
            s = v.state
            if not v.link_ok(5.0) and self.state != "LINK_LOST":
                self.log("LINK_LOST", "no heartbeat for 5 s: ArduPilot's GCS failsafe decides (RTL)")
            elif v.link_ok(5.0) and self.state == "LINK_LOST":
                self.log("AUTO" if s.mode == "AUTO" else s.mode, "link back")
            if s.mission_seq != last_seq and s.mode == "AUTO":
                last_seq = s.mission_seq
            if s.armed and s.mode not in ("AUTO", "GUIDED") and interrupted is None and not self._abort.is_set():
                interrupted = s.mode
                why = next((t for _, sev, t in reversed(list(s.texts)) if sev <= 4), "")
                self.log(s.mode, f"the aircraft left AUTO ({s.mode}){': ' + why if why else ''}: waiting for it to land")
            if not s.armed:
                if interrupted:
                    self.log("LANDED", f"sortie {self.sortie + 1} interrupted ({interrupted})")
                    raise VehicleError(f"sortie {self.sortie + 1} interrupted by {interrupted}")
                self.log("LANDED", f"sortie {self.sortie + 1} complete")
                return
            time.sleep(0.2)
        raise VehicleError("sortie did not end within 3 h")
