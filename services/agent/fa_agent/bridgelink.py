"""The companion side of the S-BUS bridge: frames to and from the RP2040, and the bridge's
own C core loaded as a shared library (for simulation, so the logic tested is the logic
flown).

Wire format (firmware/sbus_bridge/src/bridge_core.h):
    0xA5 | type | len | payload | crc16-CCITT-FALSE(type, len, payload) little endian
"""
from __future__ import annotations

import ctypes
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SYNC = 0xA5
CHANNELS, CONFIG, PING, STATUS = 0x01, 0x02, 0x03, 0x81
NCH = 16
SOURCES = {0: "none", 1: "pilot", 2: "companion", 3: "hold", 4: "rth"}
FIRMWARE = Path(__file__).resolve().parents[3] / "firmware" / "sbus_bridge"


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode(kind: int, payload: bytes) -> bytes:
    if len(payload) > 64:
        raise ValueError("payload too long")
    body = bytes([kind, len(payload)]) + payload
    return bytes([SYNC]) + body + struct.pack("<H", crc16(body))


def channels_frame(us: list[int], ttl_ms: int, seq: int) -> bytes:
    if len(us) != NCH:
        raise ValueError("16 channels")
    vals = [int(min(2200, max(800, round(v)))) for v in us]
    return encode(CHANNELS, struct.pack("<HH16H", seq & 0xFFFF, ttl_ms, *vals))


@dataclass
class BridgeConfig:
    """Mirror of bridge_config_t (defaults identical to bridge_config_default)."""
    auto_ch: int = 7
    auto_on_us: int = 1700
    pilot_timeout_ms: int = 200
    max_ttl_ms: int = 250
    hold_ms: int = 2000
    mode_ch: int = 4
    mode_gps_us: int = 1500
    rth_ch: int = 5
    rth_on_us: int = 2000
    rth_off_us: int = 1000
    throttle_ch: int = 2
    pump_ch: int = 6
    pump_off_us: int = 1000

    def pack(self) -> bytes:
        return struct.pack("<BHHHHBHBHHBBH", self.auto_ch, self.auto_on_us, self.pilot_timeout_ms,
                           self.max_ttl_ms, self.hold_ms, self.mode_ch, self.mode_gps_us, self.rth_ch,
                           self.rth_on_us, self.rth_off_us, self.throttle_ch, self.pump_ch, self.pump_off_us)


@dataclass
class BridgeStatus:
    source: str
    pilot_ok: bool
    pilot_failsafe: bool
    auto_allowed: bool
    companion_fresh: bool
    last_seq: int
    frames_out: int
    link_errors: int
    pilot_good: int
    pilot_bad: int
    at: float = 0.0

    @classmethod
    def unpack(cls, p: bytes, at: float = 0.0) -> "BridgeStatus":
        src, fl, seq, out, errs, good, bad = struct.unpack("<BBHIHII", p[:18])
        return cls(SOURCES.get(src, "?"), bool(fl & 1), bool(fl & 2), bool(fl & 4), bool(fl & 8), seq, out,
                   errs, good, bad, at)


class FrameParser:
    def __init__(self):
        self.buf = bytearray()
        self.good = self.bad = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        out = []
        self.buf += data
        while True:
            i = self.buf.find(bytes([SYNC]))
            if i < 0:
                self.buf.clear()
                return out
            del self.buf[:i]
            if len(self.buf) < 3:
                return out
            n = self.buf[2]
            if n > 64:
                self.bad += 1
                del self.buf[:1]
                continue
            if len(self.buf) < n + 5:
                return out
            body, crc = bytes(self.buf[1:3 + n]), struct.unpack("<H", self.buf[3 + n:5 + n])[0]
            if crc16(body) == crc:
                self.good += 1
                out.append((body[0], body[2:]))
                del self.buf[:n + 5]
            else:
                self.bad += 1
                del self.buf[:1]


class SerialBridge:
    """The real bridge over a serial port (Pi UART /dev/serial0 or the Pico's USB CDC)."""

    def __init__(self, port: str, baud: int = 115200):
        import serial  # pyserial
        self.ser = serial.Serial(port, baud, timeout=0)
        self.parser = FrameParser()
        self.status: BridgeStatus | None = None
        self.seq = 0
        self._lock = threading.Lock()

    def send(self, us: list[int], ttl_ms: int = 200) -> None:
        with self._lock:
            self.seq = (self.seq + 1) & 0xFFFF
            self.ser.write(channels_frame(us, ttl_ms, self.seq))

    def configure(self, cfg: BridgeConfig) -> None:
        self.ser.write(encode(CONFIG, cfg.pack()))

    def poll(self) -> BridgeStatus | None:
        data = self.ser.read(4096)
        for kind, p in self.parser.feed(data):
            if kind == STATUS and len(p) >= 18:
                self.status = BridgeStatus.unpack(p, time.monotonic())
        return self.status


# ---------------------------------------------------------------- the C core (simulation)
class _Frame(ctypes.Structure):
    _fields_ = [("us", ctypes.c_uint16 * NCH), ("ch17", ctypes.c_bool), ("ch18", ctypes.c_bool),
                ("frame_lost", ctypes.c_bool), ("failsafe", ctypes.c_bool)]


def _lib_path() -> Path:
    lib = FIRMWARE / "build-host" / "libbridgecore.so"
    src = FIRMWARE / "src" / "bridge_core.c"
    if not lib.exists() or lib.stat().st_mtime < src.stat().st_mtime:
        subprocess.run(["make", "-s", "-C", str(FIRMWARE), "-f", "host.mk", "lib"], check=True)
    return lib


class CoreBridge:
    """The firmware's bridge_core.c, driven in simulated time."""

    _lib = None

    def __init__(self, cfg: BridgeConfig | None = None):
        if CoreBridge._lib is None:
            lib = ctypes.CDLL(str(_lib_path()))
            lib.bridge_output.restype = ctypes.c_int
            CoreBridge._lib = lib
        self.lib = CoreBridge._lib
        self.cfg = cfg or BridgeConfig()
        self._state = ctypes.create_string_buffer(1024)       # > sizeof(bridge_t)
        c = ctypes.create_string_buffer(64)
        self.lib.bridge_config_default(c)
        packed = self.cfg.pack()
        ok = self.lib.bridge_config_unpack(packed, len(packed), c)
        if not ok:
            raise ValueError("bridge config rejected by the firmware core")
        self.lib.bridge_init(self._state, c)
        self.seq = 0

    def pilot(self, us: list[int], now_ms: int, failsafe: bool = False) -> None:
        f = _Frame()
        f.us[:] = [int(v) for v in us]
        f.failsafe = failsafe
        self.lib.bridge_on_pilot(self._state, ctypes.byref(f), ctypes.c_uint32(now_ms))

    def companion(self, us: list[int], ttl_ms: int, now_ms: int) -> None:
        self.seq = (self.seq + 1) & 0xFFFF
        arr = (ctypes.c_uint16 * NCH)(*[int(v) for v in us])
        self.lib.bridge_on_companion(self._state, arr, ctypes.c_uint16(ttl_ms), ctypes.c_uint16(self.seq),
                                     ctypes.c_uint32(now_ms))

    def output(self, now_ms: int) -> tuple[str, list[int] | None]:
        f, emit = _Frame(), ctypes.c_bool()
        src = self.lib.bridge_output(self._state, ctypes.c_uint32(now_ms), ctypes.byref(f), ctypes.byref(emit))
        return SOURCES[src], (list(f.us) if emit.value else None)

    def status(self, now_ms: int) -> BridgeStatus:
        buf = ctypes.create_string_buffer(32)
        n = self.lib.bridge_status(self._state, ctypes.c_uint32(now_ms), buf, 32)
        return BridgeStatus.unpack(buf.raw[:n], now_ms / 1000.0)
