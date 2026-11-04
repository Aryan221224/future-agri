"""Start ArduPilot SITL (the real ArduCopter code, simulated physics) for end-to-end tests.

    FA_SITL=/path/to/ardupilot/build/sitl/bin/arducopter python -m pytest tests/test_aircraft_sitl.py
Build it once: git clone ardupilot; ./waf configure --board sitl; ./waf copter.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def find_sitl() -> str | None:
    p = os.environ.get("FA_SITL")
    if p and Path(p).exists():
        return p
    return shutil.which("arducopter")


class Sitl:
    def __init__(self, binary: str, home=(28.47, 77.50, 200.0, 0.0), speedup: int = 5, instance: int = 0,
                 defaults: str | None = None):
        self.binary, self.home, self.speedup, self.instance = binary, home, speedup, instance
        self.defaults = defaults or str(Path(binary).resolve().parents[3] / "Tools" / "autotest" /
                                        "default_params" / "copter.parm")
        self.dir = tempfile.mkdtemp(prefix="fa_sitl_")
        self.proc: subprocess.Popen | None = None

    @property
    def port(self) -> int:
        return 5760 + 10 * self.instance

    def start(self, timeout_s: float = 30.0) -> "Sitl":
        args = [self.binary, "--model", "quad", "--speedup", str(self.speedup), "-I", str(self.instance),
                "--home", ",".join(str(x) for x in self.home), "--defaults", self.defaults, "-w"]
        self.proc = subprocess.Popen(args, cwd=self.dir, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(f"SITL exited with {self.proc.returncode}")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return self
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("SITL did not open its TCP port")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)
