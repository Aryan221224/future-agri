"""Hardware on the companion computer: the INA228 battery monitor and the pump cut relay.

Both degrade safely: no I2C device -> no power reading (the pilot's preflight refuses to
arm when the power monitor is required); no GPIO -> the relay is reported missing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .sim import Power


@dataclass
class INA228:
    """TI INA228 on I2C (85 V bus, 20-bit): a 12S battery up to 50.4 V and 100+ A through
    a 0.5 mOhm shunt."""
    bus: int = 1
    addr: int = 0x40
    shunt_ohm: float = 0.0005
    max_current_a: float = 150.0

    def __post_init__(self):
        from smbus2 import SMBus  # optional dependency on the Pi
        self.i2c = SMBus(self.bus)
        self.current_lsb = self.max_current_a / 2 ** 19
        cal = int(13107.2e6 * self.current_lsb * self.shunt_ohm)
        self._write16(0x02, cal)                       # SHUNT_CAL
        self._write16(0x01, 0xFB6A)                    # ADC_CONFIG: continuous bus/shunt/temp, 1052 us, 16 avg

    def _write16(self, reg: int, v: int) -> None:
        self.i2c.write_i2c_block_data(self.addr, reg, [(v >> 8) & 0xFF, v & 0xFF])

    def _read24(self, reg: int) -> int:
        b = self.i2c.read_i2c_block_data(self.addr, reg, 3)
        return (b[0] << 16 | b[1] << 8 | b[2]) >> 4

    def read(self) -> Power:
        vbus = self._read24(0x05) * 195.3125e-6
        raw = self._read24(0x07)
        if raw & 0x80000:
            raw -= 1 << 20
        return Power(vbus, raw * self.current_lsb, time.monotonic())


class PumpRelay:
    """A relay (or MOSFET) in the 24 V pump line: closed = the pump may run. Opens on start,
    on exit and whenever the agent is not spraying."""

    def __init__(self, chip: str = "gpiochip0", line: int = 17, active_high: bool = True):
        import gpiod  # libgpiod python bindings on the Pi
        self._chip = gpiod.Chip(chip)
        self._line = self._chip.get_line(line)
        self._line.request(consumer="fa-agent", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0 if active_high else 1])
        self.active_high = active_high
        self.closed = False

    def set(self, allow: bool) -> None:
        if allow != self.closed:
            self._line.set_value(1 if allow == self.active_high else 0)
            self.closed = allow

    def close(self) -> None:
        self.set(False)
        self._line.release()
