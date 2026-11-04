"""RTK position and heading from a u-blox ZED-F9P moving-base pair, and RTCM corrections.

* Front receiver (moving base): RTCM from the base station or NTRIP -> RTK position (NAV-PVT).
* Rear receiver (rover): RTCM from the front one -> baseline heading (NAV-RELPOSNED).
Both stream UBX; one parser reads either. Local east/north/up are metres from home.
"""
from __future__ import annotations

import base64
import math
import socket
import struct
import threading
import time
from dataclasses import dataclass

from .route import Local
from .sim import Fix

SYNC1, SYNC2 = 0xB5, 0x62
NAV_PVT, NAV_RELPOSNED = (0x01, 0x07), (0x01, 0x3C)


def ubx_checksum(body: bytes) -> bytes:
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return bytes([a, b])


def ubx_frame(cls: int, mid: int, payload: bytes) -> bytes:
    body = bytes([cls, mid]) + struct.pack("<H", len(payload)) + payload
    return bytes([SYNC1, SYNC2]) + body + ubx_checksum(body)


class UbxParser:
    def __init__(self):
        self.buf = bytearray()
        self.bad = 0

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(bytes([SYNC1, SYNC2]))
            if i < 0:
                del self.buf[:-1]
                return out
            del self.buf[:i]
            if len(self.buf) < 6:
                return out
            n = struct.unpack_from("<H", self.buf, 4)[0]
            if n > 1024:
                self.bad += 1
                del self.buf[:2]
                continue
            if len(self.buf) < n + 8:
                return out
            body = bytes(self.buf[2:6 + n])
            if ubx_checksum(body) == bytes(self.buf[6 + n:8 + n]):
                out.append((body[0], body[1], body[4:]))
                del self.buf[:n + 8]
            else:
                self.bad += 1
                del self.buf[:2]


@dataclass
class Pvt:
    t: float
    lat: float
    lon: float
    h_ell: float
    v_n: float
    v_e: float
    v_d: float
    fix_type: int
    carr: int          # 0 none, 1 float, 2 fixed
    sats: int
    h_acc: float

    @classmethod
    def parse(cls, p: bytes, t: float) -> "Pvt":
        if len(p) < 92:
            raise ValueError("NAV-PVT too short")
        fix_type, flags, _, sats = p[20], p[21], p[22], p[23]
        lon, lat, h, _hmsl, h_acc, _v_acc, vn, ve, vd = struct.unpack_from("<iiiiIIiii", p, 24)
        ok = flags & 0x01
        return cls(t, lat * 1e-7, lon * 1e-7, h / 1000.0, vn / 1000.0, ve / 1000.0, vd / 1000.0,
                   fix_type if ok else 0, (flags >> 6) & 0x03, sats, h_acc / 1000.0)


@dataclass
class RelPos:
    t: float
    heading: float     # deg, baseline direction clockwise from north
    length_m: float
    valid: bool
    carr: int

    @classmethod
    def parse(cls, p: bytes, t: float) -> "RelPos":
        if len(p) < 64:
            raise ValueError("NAV-RELPOSNED too short")
        length_cm, heading = struct.unpack_from("<ii", p, 20)
        flags = struct.unpack_from("<I", p, 60)[0]
        valid = bool(flags & 0x01) and bool(flags & 0x04) and bool(flags & 0x100)
        return cls(t, heading * 1e-5, length_cm / 100.0, valid, (flags >> 3) & 0x03)


class FixBuilder:
    """Combines the latest NAV-PVT and NAV-RELPOSNED into a Fix in local metres."""

    def __init__(self, home_lat: float, home_lon: float, home_h: float | None = None,
                 heading_offset_deg: float = 0.0, baseline_m: float | None = None,
                 baseline_tol_m: float = 0.05, max_age_s: float = 0.5):
        self.local = Local(home_lat, home_lon)
        self.home_h = home_h
        self.offset = heading_offset_deg
        self.baseline_m, self.tol = baseline_m, baseline_tol_m
        self.max_age = max_age_s
        self.pvt: Pvt | None = None
        self.rel: RelPos | None = None

    def on_ubx(self, cls: int, mid: int, p: bytes, t: float) -> None:
        if (cls, mid) == NAV_PVT:
            self.pvt = Pvt.parse(p, t)
            if self.home_h is None and self.pvt.carr == 2:
                self.home_h = self.pvt.h_ell            # height reference: first RTK fix at home
        elif (cls, mid) == NAV_RELPOSNED:
            self.rel = RelPos.parse(p, t)

    def fix(self, now: float) -> Fix | None:
        p = self.pvt
        if p is None or now - p.t > self.max_age or p.fix_type < 3:
            return None
        kind = {2: "fixed", 1: "float"}.get(p.carr, "3d")
        e, n = self.local.to_en(p.lon, p.lat)
        r = self.rel
        hd_ok = (r is not None and r.valid and now - r.t <= self.max_age and r.carr == 2 and
                 (self.baseline_m is None or abs(r.length_m - self.baseline_m) <= self.tol))
        heading = ((r.heading + self.offset) % 360.0) if r is not None else 0.0
        h = p.h_ell - (self.home_h if self.home_h is not None else p.h_ell)
        return Fix(p.t, e, n, h, p.v_e, p.v_n, -p.v_d, heading, hd_ok, kind, p.sats)


# ---------------------------------------------------------------- NTRIP corrections
def gga(lat: float, lon: float, h: float, sats: int = 12, utc: float | None = None) -> bytes:
    """An NMEA GGA sentence (casters with virtual reference stations need the rover's position)."""
    t = time.gmtime(utc if utc is not None else time.time())
    la, lo = abs(lat), abs(lon)
    body = "GPGGA,%02d%02d%02d.00,%02d%09.6f,%s,%03d%09.6f,%s,1,%02d,1.0,%.1f,M,0.0,M,," % (
        t.tm_hour, t.tm_min, t.tm_sec, int(la), (la - int(la)) * 60, "N" if lat >= 0 else "S",
        int(lo), (lo - int(lo)) * 60, "E" if lon >= 0 else "W", sats, h)
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n".encode()


class NtripClient:
    """Streams RTCM from an NTRIP caster to ``sink(bytes)``; reconnects with back-off and sends
    a GGA every 10 s when ``position()`` gives one. Runs in its own thread."""

    def __init__(self, host: str, port: int, mount: str, user: str = "", password: str = "",
                 sink=None, position=None, timeout_s: float = 10.0):
        self.host, self.port, self.mount = host, port, mount
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode() if user else None
        self.sink, self.position, self.timeout = sink, position, timeout_s
        self.bytes_in = 0
        self.last_rx = 0.0
        self.error: str | None = None
        self._stop = threading.Event()

    def request(self) -> bytes:
        lines = [f"GET /{self.mount} HTTP/1.0", f"Host: {self.host}", "User-Agent: NTRIP fa-agent/1.0",
                 "Accept: */*", "Connection: close"]
        if self.auth:
            lines.append(f"Authorization: Basic {self.auth}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def run_once(self) -> None:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(self.request())
            head = b""
            while b"\r\n" not in head:
                chunk = s.recv(256)
                if not chunk:
                    raise ConnectionError("caster closed the connection")
                head += chunk
            status = head.split(b"\r\n", 1)[0]
            if b"200" not in status:
                raise PermissionError(f"caster refused: {status.decode(errors='replace')}")
            rest = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
            if rest and self.sink:
                self.sink(rest)
            next_gga = 0.0
            while not self._stop.is_set():
                if self.position is not None and time.monotonic() >= next_gga:
                    pos = self.position()
                    if pos is not None:
                        s.sendall(gga(*pos))
                    next_gga = time.monotonic() + 10.0
                data = s.recv(4096)
                if not data:
                    raise ConnectionError("caster stream ended")
                self.bytes_in += len(data)
                self.last_rx = time.monotonic()
                if self.sink:
                    self.sink(data)

    def run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                self.run_once()
                delay = 1.0
            except Exception as e:  # noqa: BLE001 -- keep trying; the agent watches the RTK status
                self.error = f"{type(e).__name__}: {e}"
                self._stop.wait(delay)
                delay = min(30.0, delay * 2)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="ntrip", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()


def heading_from_baseline(relpos_n: float, relpos_e: float) -> float:
    return math.degrees(math.atan2(relpos_e, relpos_n)) % 360.0
