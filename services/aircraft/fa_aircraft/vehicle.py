"""An ArduPilot Copter over MAVLink: connect over any link, check it, upload, fly.

Links (pymavlink connection strings):
    serial:COM5:57600  /  serial:/dev/ttyUSB0:57600   USB cable or SiK telemetry radio
    udpin:0.0.0.0:14550                              the aircraft (or mavlink-router) sends to us
    udpout:192.168.4.1:14550                         we send first (some Wi-Fi bridges)
    tcp:192.168.4.1:5760                             mavlink-router / SITL TCP
Everything that changes the aircraft waits for its acknowledgement and raises VehicleError
with ArduPilot's own reason (COMMAND_ACK result and the PreArm/statustext lines).
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from pymavlink import mavutil

M = mavutil.mavlink
COPTER_MODES = {"STABILIZE": 0, "ACRO": 1, "ALT_HOLD": 2, "AUTO": 3, "GUIDED": 4, "LOITER": 5, "RTL": 6,
                "CIRCLE": 7, "LAND": 9, "DRIFT": 11, "SPORT": 13, "POSHOLD": 16, "BRAKE": 17, "SMART_RTL": 21}
MODE_NAMES = {v: k for k, v in COPTER_MODES.items()}
ACK_TEXT = {0: "accepted", 1: "temporarily rejected", 2: "denied", 3: "unsupported", 4: "failed",
            5: "in progress", 6: "cancelled"}


class VehicleError(RuntimeError):
    pass


def parse_link(link: str) -> tuple[str, dict]:
    """'serial:COM5:57600' -> ('COM5', {'baud': 57600}); udpin/udpout/tcp pass through."""
    if link.startswith("serial:"):
        rest = link[len("serial:"):]
        port, _, baud = rest.rpartition(":")
        if not port:
            port, baud = rest, "57600"
        return port, {"baud": int(baud)}
    if link.split(":", 1)[0] in ("udpin", "udpout", "tcp", "tcpin", "udp"):
        return link, {}
    raise ValueError(f"unknown link {link!r}: use serial:PORT:BAUD, udpin:IP:PORT, udpout:IP:PORT or tcp:IP:PORT")


@dataclass
class State:
    heartbeat_at: float = 0.0
    mode: str = "?"
    armed: bool = False
    system_status: int = 0
    lat: float | None = None
    lon: float | None = None
    rel_alt: float | None = None
    vx: float = 0.0
    vy: float = 0.0
    vz: float = 0.0
    heading: float = 0.0
    gps_fix: int = 0
    sats: int = 0
    hdop: float = 99.0
    voltage: float | None = None
    current: float | None = None
    remaining: int | None = None
    ekf_flags: int = 0
    rc_ok: bool | None = None
    mission_seq: int = -1
    mission_reached: int = -1
    home: tuple | None = None
    landed_state: int = 0
    texts: deque = field(default_factory=lambda: deque(maxlen=60))

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("mode", "armed", "lat", "lon", "rel_alt", "vx", "vy", "vz", "heading",
                                           "gps_fix", "sats", "hdop", "voltage", "current", "remaining", "rc_ok",
                                           "mission_seq", "mission_reached", "home", "landed_state")}
        d["link_age_s"] = round(time.monotonic() - self.heartbeat_at, 1) if self.heartbeat_at else None
        d["texts"] = list(self.texts)[-10:]
        return d


class Vehicle:
    def __init__(self, link: str, source_system: int = 255, timeout_s: float = 30.0, heartbeat_hz: float = 1.0):
        dev, kw = parse_link(link)
        self.link = link
        self.heartbeat_hz = heartbeat_hz        # raise it for SITL with --speedup (GCS failsafe counts sim time)
        self.mav = mavutil.mavlink_connection(dev, source_system=source_system, source_component=190,
                                              autoreconnect=True, **kw)
        self.state = State()
        self._cv = threading.Condition()
        self._inbox: deque = deque(maxlen=500)
        self._stop = threading.Event()
        self.target = None
        self._rx = threading.Thread(target=self._recv, daemon=True, name="mavlink rx")
        self._hb = threading.Thread(target=self._heartbeat, daemon=True, name="gcs heartbeat")
        self._rx.start()
        self._hb.start()
        self.wait_heartbeat(timeout_s)
        self._streams()

    # ---------------------------------------------------------------- plumbing
    def close(self) -> None:
        self._stop.set()
        try:
            self.mav.close()
        except Exception:  # noqa: BLE001
            pass

    def _heartbeat(self) -> None:
        while not self._stop.is_set():
            try:
                self.mav.mav.heartbeat_send(M.MAV_TYPE_GCS, M.MAV_AUTOPILOT_INVALID, 0, 0, M.MAV_STATE_ACTIVE)
            except Exception:  # noqa: BLE001 -- link down: reconnect is pymavlink's job
                pass
            self._stop.wait(1.0 / self.heartbeat_hz)

    def _recv(self) -> None:
        while not self._stop.is_set():
            try:
                m = self.mav.recv_match(blocking=True, timeout=0.5)
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
                continue
            if m is None or m.get_type() == "BAD_DATA":
                continue
            self._update(m)
            with self._cv:
                self._inbox.append(m)
                self._cv.notify_all()

    def _update(self, m) -> None:
        s, t = self.state, m.get_type()
        if t == "HEARTBEAT":
            if m.type == M.MAV_TYPE_GCS or m.autopilot == M.MAV_AUTOPILOT_INVALID:
                return
            if self.target is None:
                self.target = (m.get_srcSystem(), m.get_srcComponent())
            s.heartbeat_at = time.monotonic()
            s.armed = bool(m.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED)
            s.mode = MODE_NAMES.get(m.custom_mode, str(m.custom_mode))
            s.system_status = m.system_status
        elif t == "GLOBAL_POSITION_INT":
            s.lat, s.lon, s.rel_alt = m.lat * 1e-7, m.lon * 1e-7, m.relative_alt / 1000.0
            s.vx, s.vy, s.vz = m.vx / 100.0, m.vy / 100.0, m.vz / 100.0
            s.heading = (m.hdg / 100.0) if m.hdg != 65535 else s.heading
        elif t == "GPS_RAW_INT":
            s.gps_fix, s.sats = m.fix_type, m.satellites_visible
            s.hdop = m.eph / 100.0 if m.eph != 65535 else 99.0
        elif t == "SYS_STATUS":
            s.voltage = m.voltage_battery / 1000.0 if m.voltage_battery != 65535 else None
            s.current = m.current_battery / 100.0 if m.current_battery >= 0 else None
            s.remaining = m.battery_remaining if m.battery_remaining >= 0 else None
            rc = M.MAV_SYS_STATUS_SENSOR_RC_RECEIVER
            if m.onboard_control_sensors_present & rc:
                s.rc_ok = bool(m.onboard_control_sensors_health & rc)
        elif t == "EKF_STATUS_REPORT":
            s.ekf_flags = m.flags
        elif t == "MISSION_CURRENT":
            s.mission_seq = m.seq
        elif t == "MISSION_ITEM_REACHED":
            s.mission_reached = m.seq
        elif t == "HOME_POSITION":
            s.home = (m.latitude * 1e-7, m.longitude * 1e-7, m.altitude / 1000.0)
        elif t == "EXTENDED_SYS_STATE":
            s.landed_state = m.landed_state
        elif t == "STATUSTEXT":
            s.texts.append((round(time.time(), 1), m.severity, m.text))

    def _wait(self, pred, timeout: float, what: str):
        end = time.monotonic() + timeout
        with self._cv:
            while True:
                for i, m in enumerate(self._inbox):
                    if pred(m):
                        del self._inbox[i]
                        return m
                left = end - time.monotonic()
                if left <= 0:
                    raise VehicleError(f"timed out waiting for {what}")
                self._cv.wait(min(left, 0.2))

    def _clear(self, *types: str) -> None:
        with self._cv:
            for m in [m for m in self._inbox if m.get_type() in types]:
                self._inbox.remove(m)

    @property
    def ts(self) -> int:
        return self.target[0]

    @property
    def tc(self) -> int:
        return self.target[1]

    def wait_heartbeat(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while self.state.heartbeat_at == 0.0:
            if time.monotonic() > end:
                raise VehicleError(f"no heartbeat from an autopilot on {self.link} within {timeout:.0f} s "
                                   "(wrong port or baud rate? radio not paired? aircraft powered?)")
            time.sleep(0.1)

    def link_ok(self, max_age_s: float = 3.0) -> bool:
        return self.state.heartbeat_at > 0 and time.monotonic() - self.state.heartbeat_at < max_age_s

    def _streams(self) -> None:
        for msg_id, hz in ((M.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, 5), (M.MAVLINK_MSG_ID_GPS_RAW_INT, 2),
                           (M.MAVLINK_MSG_ID_SYS_STATUS, 2), (M.MAVLINK_MSG_ID_EKF_STATUS_REPORT, 1),
                           (M.MAVLINK_MSG_ID_MISSION_CURRENT, 2), (M.MAVLINK_MSG_ID_EXTENDED_SYS_STATE, 1),
                           (M.MAVLINK_MSG_ID_HOME_POSITION, 0.2)):
            self.mav.mav.command_long_send(self.ts, self.tc, M.MAV_CMD_SET_MESSAGE_INTERVAL, 0, msg_id,
                                           1e6 / hz, 0, 0, 0, 0, 0)
        self.mav.mav.request_data_stream_send(self.ts, self.tc, M.MAV_DATA_STREAM_ALL, 4, 1)   # older firmware

    # ---------------------------------------------------------------- commands
    def command(self, cmd: int, *params: float, timeout: float = 5.0, retries: int = 3) -> None:
        p = list(params) + [0.0] * (7 - len(params))
        last = None
        for attempt in range(retries):
            self._clear("COMMAND_ACK")
            self.mav.mav.command_long_send(self.ts, self.tc, cmd, attempt, *p)
            try:
                ack = self._wait(lambda m: m.get_type() == "COMMAND_ACK" and m.command == cmd, timeout,
                                 f"acknowledgement of command {cmd}")
            except VehicleError as e:
                last = e
                continue
            if ack.result == M.MAV_RESULT_ACCEPTED:
                return
            if ack.result == M.MAV_RESULT_IN_PROGRESS:
                continue
            raise VehicleError(f"command {cmd} {ACK_TEXT.get(ack.result, ack.result)}{self._why()}")
        raise last or VehicleError(f"command {cmd} not accepted")

    def _why(self) -> str:
        recent = [t for at, sev, t in list(self.state.texts)[-8:] if time.time() - at < 5 and sev <= 4 or
                  t.startswith(("PreArm", "Arm"))]
        return (": " + "; ".join(dict.fromkeys(recent))) if recent else ""

    def set_mode(self, name: str, timeout: float = 5.0) -> None:
        mode = COPTER_MODES[name]
        self.command(M.MAV_CMD_DO_SET_MODE, M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode, timeout=timeout)
        end = time.monotonic() + timeout
        while self.state.mode != name:
            if time.monotonic() > end:
                raise VehicleError(f"the aircraft did not enter {name} (still {self.state.mode}){self._why()}")
            time.sleep(0.1)

    def arm(self, timeout: float = 10.0) -> None:
        self.command(M.MAV_CMD_COMPONENT_ARM_DISARM, 1, timeout=timeout)
        end = time.monotonic() + timeout
        while not self.state.armed:
            if time.monotonic() > end:
                raise VehicleError(f"not armed{self._why()}")
            time.sleep(0.1)

    def disarm(self) -> None:
        self.command(M.MAV_CMD_COMPONENT_ARM_DISARM, 0)

    def takeoff(self, alt_m: float, timeout: float = 60.0) -> None:
        self.command(M.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, math.nan, math.nan, math.nan, alt_m)
        end = time.monotonic() + timeout
        while (self.state.rel_alt or 0.0) < alt_m * 0.9:
            if time.monotonic() > end:
                raise VehicleError(f"take-off stalled at {self.state.rel_alt} m")
            if not self.state.armed:
                raise VehicleError(f"disarmed during take-off{self._why()}")
            time.sleep(0.1)

    def start_mission(self) -> None:
        self.set_mode("AUTO")
        self.command(M.MAV_CMD_MISSION_START, 0, 0)

    # ---------------------------------------------------------------- parameters
    def get_param(self, name: str, timeout: float = 3.0, retries: int = 3) -> float:
        for _ in range(retries):
            self._clear("PARAM_VALUE")
            self.mav.mav.param_request_read_send(self.ts, self.tc, name.encode(), -1)
            try:
                m = self._wait(lambda m: m.get_type() == "PARAM_VALUE" and m.param_id == name, timeout,
                               f"parameter {name}")
                return float(m.param_value)
            except VehicleError:
                continue
        raise VehicleError(f"parameter {name} not found (wrong firmware?)")

    def set_param(self, name: str, value: float, timeout: float = 3.0) -> None:
        for _ in range(3):
            self._clear("PARAM_VALUE")
            self.mav.mav.param_set_send(self.ts, self.tc, name.encode(), float(value), M.MAV_PARAM_TYPE_REAL32)
            try:
                m = self._wait(lambda m: m.get_type() == "PARAM_VALUE" and m.param_id == name, timeout, name)
            except VehicleError:
                continue
            if abs(m.param_value - value) <= 1e-4 * max(1.0, abs(value)):
                return
            raise VehicleError(f"{name} stayed {m.param_value} (asked {value})")
        raise VehicleError(f"no answer setting {name}")

    def check_params(self, expected: dict[str, float]) -> list[str]:
        bad = []
        for k, v in expected.items():
            try:
                got = self.get_param(k)
            except VehicleError as e:
                bad.append(str(e))
                continue
            if abs(got - v) > 1e-3 * max(1.0, abs(v)):
                bad.append(f"{k} = {got:g} (expected {v:g})")
        return bad

    # ---------------------------------------------------------------- missions
    def upload(self, items, mission_type: int = M.MAV_MISSION_TYPE_MISSION, timeout: float = 4.0,
               verify: bool = True) -> None:
        n = len(items)
        for attempt in range(3):
            self._clear("MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK")
            self.mav.mav.mission_count_send(self.ts, self.tc, n, mission_type)
            sent = set()
            try:
                while True:
                    m = self._wait(lambda m: m.get_type() in ("MISSION_REQUEST", "MISSION_REQUEST_INT",
                                                              "MISSION_ACK") and
                                   getattr(m, "mission_type", 0) == mission_type, timeout,
                                   "the aircraft to request mission items")
                    if m.get_type() == "MISSION_ACK":
                        if m.type == M.MAV_MISSION_ACCEPTED and len(sent) == n:
                            break
                        if m.type == M.MAV_MISSION_ACCEPTED and n == 0:
                            break
                        raise VehicleError(f"mission upload refused (MAV_MISSION_RESULT {m.type})")
                    if not 0 <= m.seq < n:
                        raise VehicleError(f"aircraft asked for item {m.seq} of {n}")
                    self.mav.mav.send(items[m.seq].msg(self.ts, self.tc, m.seq, mission_type))
                    sent.add(m.seq)
                break
            except VehicleError as e:
                if "refused" in str(e) or attempt == 2:
                    raise
        if verify:
            got = self.download(mission_type, timeout)
            first = 1 if mission_type == M.MAV_MISSION_TYPE_MISSION else 0   # item 0 = home, rewritten
            if len(got) != n or any(not it.same_as(g) for it, g in zip(items[first:], got[first:])):
                raise VehicleError("mission read back differs from what was sent")

    def download(self, mission_type: int = M.MAV_MISSION_TYPE_MISSION, timeout: float = 4.0) -> list:
        self._clear("MISSION_COUNT", "MISSION_ITEM_INT")
        self.mav.mav.mission_request_list_send(self.ts, self.tc, mission_type)
        c = self._wait(lambda m: m.get_type() == "MISSION_COUNT" and getattr(m, "mission_type", 0) == mission_type,
                       timeout, "the mission count")
        out = []
        for i in range(c.count):
            for attempt in range(3):
                self.mav.mav.mission_request_int_send(self.ts, self.tc, i, mission_type)
                try:
                    out.append(self._wait(lambda m: m.get_type() == "MISSION_ITEM_INT" and m.seq == i, timeout,
                                          f"mission item {i}"))
                    break
                except VehicleError:
                    if attempt == 2:
                        raise
        self.mav.mav.mission_ack_send(self.ts, self.tc, M.MAV_MISSION_ACCEPTED, mission_type)
        return out
