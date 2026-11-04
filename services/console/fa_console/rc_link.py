"""The Android remote controller (or phone) that runs JIYI Agri Assistant, over ADB.

Wired: USB cable from this computer to the remote, USB debugging on. Wireless: the remote on
the same Wi-Fi, "Wireless debugging" / `adb tcpip 5555` once, then connect to its address.
The console only moves files: block KMLs to ``push_dir`` (import them in Agri Assistant) and
exported flight records from ``records_dir`` back. Nothing on the remote is tapped for you.

Needs Android platform-tools (adb) on this computer: found on PATH, at FA_ADB, or in the
usual Android SDK folders.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


class RcError(RuntimeError):
    pass


def find_adb() -> str | None:
    cands = [os.environ.get("FA_ADB"), shutil.which("adb")]
    home = Path.home()
    cands += [str(home / "AppData/Local/Android/Sdk/platform-tools/adb.exe"),
              str(home / "Android/Sdk/platform-tools/adb"), str(home / "Library/Android/sdk/platform-tools/adb"),
              "C:/platform-tools/adb.exe"]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


class RcLink:
    def __init__(self, adb: str | None = None, serial: str | None = None,
                 push_dir: str = "/sdcard/Download/FutureAgri", records_dir: str = "/sdcard/Download/FutureAgri/records",
                 runner=subprocess.run):
        self.adb = adb or find_adb()
        self.serial = serial
        self.push_dir, self.records_dir = push_dir.rstrip("/"), records_dir.rstrip("/")
        self.run = runner
        self.error: str | None = None

    # ---------------------------------------------------------------- plumbing
    def _adb(self, *args: str, timeout: float = 20.0, binary: bool = False, device: bool = True):
        if not self.adb:
            raise RcError("adb not found: install Android platform-tools "
                          "(developer.android.com/tools/releases/platform-tools) or set FA_ADB")
        cmd = [self.adb] + (["-s", self.serial] if device and self.serial else []) + list(args)
        try:
            p = self.run(cmd, capture_output=True, timeout=timeout, text=not binary)
        except FileNotFoundError as e:
            raise RcError(f"adb not runnable: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise RcError(f"adb timed out ({' '.join(args[:2])}): is the remote awake and connected?") from e
        out = p.stdout
        err = (p.stderr or "") if not binary else (p.stderr or b"").decode(errors="replace")
        if p.returncode != 0:
            msg = err.strip() or (out.strip() if isinstance(out, str) else "")
            if "unauthorized" in msg:
                raise RcError("the remote has not allowed this computer: accept the 'Allow USB debugging' prompt on it")
            if "no devices" in msg or "not found" in msg and "device" in msg:
                raise RcError("no remote connected: plug it in by USB (USB debugging on) or connect over Wi-Fi")
            if "offline" in msg:
                raise RcError("the remote is offline: unplug and re-plug it, or reconnect Wi-Fi debugging")
            raise RcError(f"adb {' '.join(args[:2])} failed: {msg}")
        return out

    # ---------------------------------------------------------------- devices
    def devices(self) -> list[tuple[str, str]]:
        out = self._adb("devices", device=False)
        rows = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                rows.append((parts[0], parts[1]))
        return rows

    def connect(self, host: str) -> str:
        """Wi-Fi ADB: host 'IP' or 'IP:PORT' (default 5555)."""
        target = host if ":" in host else f"{host}:5555"
        out = self._adb("connect", target, device=False, timeout=15)
        if "connected" not in out or "cannot" in out or "failed" in out:
            raise RcError(f"could not connect to {target}: {out.strip()} (same Wi-Fi? wireless debugging on?)")
        self.serial = target
        return target

    def pick(self) -> str:
        devs = self.devices()
        ok = [s for s, st in devs if st == "device"]
        if self.serial and self.serial in ok:
            return self.serial
        bad = [st for _, st in devs if st != "device"]
        if not ok:
            if "unauthorized" in bad:
                raise RcError("the remote has not allowed this computer: accept the 'Allow USB debugging' prompt on it")
            raise RcError("no remote connected: plug it in by USB (USB debugging on) or connect over Wi-Fi")
        if len(ok) > 1 and not self.serial:
            raise RcError(f"{len(ok)} Android devices connected ({', '.join(ok)}): choose one")
        self.serial = ok[0]
        return self.serial

    def status(self) -> dict:
        try:
            s = self.pick()
            model = self._adb("shell", "getprop", "ro.product.model", timeout=5).strip()
            self.error = None
            return {"connected": True, "serial": s, "model": model, "push_dir": self.push_dir,
                    "records_dir": self.records_dir}
        except RcError as e:
            self.error = str(e)
            return {"connected": False, "error": str(e), "adb": bool(self.adb)}

    # ---------------------------------------------------------------- files
    def push_bytes(self, data: bytes, name: str) -> str:
        if "/" in name or "\\" in name or name.startswith("."):
            raise RcError("bad file name")
        self.pick()
        self._adb("shell", "mkdir", "-p", shlex.quote(self.push_dir))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / name
            p.write_bytes(data)
            self._adb("push", str(p), f"{self.push_dir}/{name}", timeout=60)
        return f"{self.push_dir}/{name}"

    def new_records(self, seen: set[str]):
        """(name, text) of record files (.csv / .json) in records_dir not seen before."""
        self.pick()
        out = self._adb("shell", "ls", "-1", shlex.quote(self.records_dir) + "/")
        for name in sorted(x.strip() for x in out.splitlines()):
            if not name or name in seen or not name.lower().endswith((".csv", ".json")):
                continue
            raw = self._adb("exec-out", "cat", shlex.quote(f"{self.records_dir}/{name}"), binary=True, timeout=30)
            for enc in ("utf-8-sig", "gb18030", "latin-1"):
                try:
                    yield name, raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue

    def screenshot(self) -> bytes:
        self.pick()
        png = self._adb("exec-out", "screencap", "-p", binary=True, timeout=15)
        if not png.startswith(b"\x89PNG"):
            raise RcError("the remote did not return a screenshot")
        return png
