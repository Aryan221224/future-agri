"""The remote GPU worker.

    python -m fa_console.worker --server http://127.0.0.1:18099 --token-file ~/.futureagri/worker.token

It runs on the GPU server (the H200) inside the SSH session that the laptop's
``Connect-GPU-Server`` script opens with a reverse tunnel back to the console
(``ssh -R 18099:127.0.0.1:8099``). It polls the console for jobs of the kinds it
can run, downloads the built farm (and the orthomosaic, for weed detection), runs
the *same* job function the console runs locally, reports progress, and posts the
result. It needs the simulation packages, not the console's database or web stack.

Kinds: ``perfect`` (iterations: the season rehearsed many times, CPU-parallel),
``mission`` (a K3-A Pro mission over a large field) and ``detect`` (RF-DETR on an
orthomosaic; advertised only when ``FA_DETECTOR_WEIGHTS`` names a trained run and
an inference backend imports).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import pickle
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

from . import __version__
from .config import Settings
from .jobs import JobCancelled, _json_default

KINDS = ("perfect", "mission", "detect")
DEFAULT_SERVER = "http://127.0.0.1:18099"
HEARTBEAT_S = 60.0
log = logging.getLogger("fa_console.worker")


class AuthError(Exception):
    pass


# ---------------------------------------------------------------- what this machine can run
def _backend_available() -> bool:
    """The detector backend the console will ask for (PyTorch FP32 by default) can be imported here."""
    from .detect import backend
    need = {"torch": ("torch", "rfdetr"), "onnx": ("onnxruntime",), "tensorrt": ("tensorrt",)}.get(backend(), ("torch",))
    return all(importlib.util.find_spec(m) is not None for m in need)


def available_kinds(requested) -> list[str]:
    """The requested kinds this machine can actually run, in the order asked."""
    from .detect import weights_dir
    out = []
    for k in requested:
        k = k.strip()
        if k not in KINDS:
            if k:
                log.warning("unknown job kind '%s' ignored", k)
            continue
        if k == "detect" and not (weights_dir() is not None and _backend_available()):
            log.info("detect not offered: set FA_DETECTOR_WEIGHTS to an ml/runs/<name> folder with an inference backend")
            continue
        out.append(k)
    return out


def gpu_name() -> str | None:
    if not shutil.which("nvidia-smi"):
        return None
    cmd = ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]
    vis = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")[0].strip()
    if vis.isdigit():
        cmd += ["-i", vis]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        return out[0].strip() if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def default_procs() -> int:
    env = os.environ.get("FA_WORKER_PROCS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    return max(1, min(32, (os.cpu_count() or 2) - 2))      # the server is shared: leave cores free


# ---------------------------------------------------------------- the console over HTTP
class Console:
    def __init__(self, server: str, token: str, timeout: float = 60.0):
        self.server, self.token, self.timeout = server.rstrip("/"), token, timeout
        host = urllib.parse.urlparse(self.server).hostname or ""
        # the tunnel is on loopback: never send it through an HTTP proxy from the environment
        handlers = [urllib.request.ProxyHandler({})] if host in ("127.0.0.1", "localhost", "::1") else []
        self.opener = urllib.request.build_opener(*handlers)

    def _open(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body, default=_json_default).encode()
        req = urllib.request.Request(self.server + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-Worker-Token": self.token})
        try:
            return self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AuthError("the console refused this worker's token") from e
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{method} {path}: HTTP {e.code} {detail}") from e

    def call(self, method: str, path: str, body=None):
        with self._open(method, path, body) as r:
            return None if r.status == 204 else json.loads(r.read() or b"null")

    def claim(self, name: str, kinds: list[str], info: dict) -> dict | None:
        return self.call("POST", "/api/workers/claim", {"token": self.token, "worker": name, "kinds": kinds, "info": info})

    def progress(self, jid: str, frac: float, message: str) -> bool:
        r = self.call("POST", f"/api/workers/{jid}/progress",
                      {"token": self.token, "progress": max(0.0, min(1.0, float(frac))), "message": message[:500]})
        return bool((r or {}).get("cancel"))

    def complete(self, jid: str, result: dict | None = None, error: str | None = None) -> None:
        self.call("POST", f"/api/workers/{jid}/complete", {"token": self.token, "result": result, "error": error})

    def download(self, path: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self._open("GET", path) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh, 1 << 20)
        tmp.replace(dest)
        return dest


# ---------------------------------------------------------------- the console state, as much as job functions need
class _Store:
    def __init__(self):
        self.farms: dict[str, dict] = {}

    def get_farm(self, fid: str) -> dict:
        return self.farms[fid]

    def add_event(self, farm_id, kind, payload=None, actor="worker"):
        log.debug("event %s %s", kind, payload)


class _Farms:
    def __init__(self, home: Path):
        from fa_farm.build import build_virtual_farm
        self.home, self.builder = home, build_virtual_farm
        self._cache: OrderedDict[str, object] = OrderedDict()

    def path(self, fid: str) -> Path:
        return self.home / "farms" / fid / "farm.pkl"

    def load(self, fid: str):
        if fid in self._cache:
            self._cache.move_to_end(fid)
            return self._cache[fid]
        with open(self.path(fid), "rb") as fh:
            vf = pickle.load(fh)
        self._cache[fid] = vf
        while len(self._cache) > 3:
            self._cache.popitem(last=False)
        return vf

    def forget(self, fid: str) -> None:
        self._cache.pop(fid, None)


class RemoteState:
    def __init__(self, home: Path, cache: Path | None):
        self.settings = Settings(home=home, cache=cache)
        self.store = _Store()
        self.farms = _Farms(home)


class RemoteContext:
    """``JobContext`` over HTTP: progress is sent at most once a second; a cancel from the
    console stops the job at its next progress call."""

    def __init__(self, console: Console, job: dict, min_interval: float = 1.0):
        self.console, self.job, self.min_interval = console, job, min_interval
        self.last = (0.0, "started")
        self._sent = 0.0
        self._cancel = False
        self._lock = threading.Lock()

    @property
    def id(self) -> str:
        return self.job["id"]

    def check(self) -> None:
        if self._cancel:
            raise JobCancelled()

    def send(self, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._sent < self.min_interval:
                return
            self._sent = now
            frac, msg = self.last
        try:
            self._cancel = self.console.progress(self.id, frac, msg) or self._cancel
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001 -- a dropped report is not a failed job
            log.warning("progress report failed: %s", e)

    def progress(self, frac: float, message: str = "") -> None:
        self.last = (float(frac), str(message))
        self.send(force=frac >= 1.0)
        self.check()


class _Heartbeat(threading.Thread):
    """Re-sends the last progress so the console's watchdog knows a long step is alive."""

    def __init__(self, ctx: RemoteContext, every: float = HEARTBEAT_S):
        super().__init__(daemon=True, name="fa-worker-heartbeat")
        self.ctx, self.every, self.stopped = ctx, every, threading.Event()

    def run(self) -> None:
        while not self.stopped.wait(self.every):
            try:
                self.ctx.send(force=True)
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self.stopped.set()


# ---------------------------------------------------------------- the worker
class Worker:
    def __init__(self, console: Console, name: str, kinds: list[str], home: Path, cache: Path | None,
                 procs: int, poll: float = 3.0, give_up_after: float = 0.0):
        self.console, self.name, self.kinds, self.home, self.procs, self.poll = console, name, kinds, home, procs, poll
        self.give_up_after = give_up_after       # seconds unreachable before exiting (0 = keep trying)
        self.home.mkdir(parents=True, exist_ok=True)
        self.state = RemoteState(home, cache)
        self.info = {"python": platform.python_version(), "host": socket.gethostname(), "cpus": os.cpu_count(),
                     "procs": procs, "gpu": gpu_name(), "version": __version__}
        self._fns: dict = {}

    def fn(self, kind: str):
        if kind not in self._fns:
            if kind == "perfect":
                from .plans import perfect_job_factory
                self._fns[kind] = perfect_job_factory(self.state)
            elif kind == "mission":
                from .dronesvc import mission_job_factory
                self._fns[kind] = mission_job_factory(self.state)
            elif kind == "detect":
                from .detect import detect_job_factory
                self._fns[kind] = detect_job_factory(self.state, lambda job: self.home / "ortho" / f"{job['id']}.tif")
            else:
                raise ValueError(f"this worker cannot run '{kind}' jobs")
        return self._fns[kind]

    def _farm(self, job: dict, url: str) -> None:
        fid, farm = job["farm_id"], job.get("farm") or {}
        stamp = self.state.farms.path(fid).with_name("farm.json")
        have = json.loads(stamp.read_text()).get("updated_at") if stamp.exists() else None
        if have != farm.get("updated_at") or not self.state.farms.path(fid).exists():
            log.info("downloading farm %s (%s)", farm.get("name", fid), fid)
            self.console.download(url, self.state.farms.path(fid))
            stamp.write_text(json.dumps({"updated_at": farm.get("updated_at"), "name": farm.get("name")}))
            self.state.farms.forget(fid)

    def run(self, job: dict) -> None:
        jid, kind = job["id"], job["kind"]
        log.info("job %s: %s for %s", jid, kind, (job.get("farm") or {}).get("name", job.get("farm_id")))
        ctx = RemoteContext(self.console, job)
        hb = _Heartbeat(ctx)
        hb.start()
        ortho = self.home / "ortho" / f"{jid}.tif"
        try:
            self.state.settings.offline = bool((job.get("console") or {}).get("offline", False))
            if job.get("farm"):
                self.state.store.farms[job["farm_id"]] = job["farm"]
            files = job.get("files") or {}
            if "farm" in files:
                ctx.progress(0.0, "fetching the farm from the console")
                self._farm(job, files["farm"])
            if "ortho" in files:
                ctx.progress(0.0, "fetching the orthomosaic from the console")
                self.console.download(files["ortho"], ortho)
            if kind == "perfect" and not job["params"].get("workers"):
                job = {**job, "params": {**job["params"], "workers": self.procs}}
            result = self.fn(kind)(job, ctx) or {}
            self._post(lambda: self.console.complete(jid, result=result), f"the result of {jid}")
            log.info("job %s done", jid)
        except JobCancelled:
            log.info("job %s cancelled by the operator", jid)
            self._fail(jid, "cancelled by the operator")
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001 -- report it; keep serving
            log.error("job %s failed\n%s", jid, traceback.format_exc())
            self._fail(jid, f"{type(e).__name__}: {e} (on {self.name})")
        finally:
            hb.stop()
            ortho.unlink(missing_ok=True)

    def _fail(self, jid: str, error: str) -> None:
        try:
            self._post(lambda: self.console.complete(jid, error=error), f"the failure of {jid}")
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("could not report the failure of %s: %s", jid, e)

    def _post(self, send, what: str) -> None:
        """Send a finished job back, retrying through a short tunnel outage."""
        deadline = time.monotonic() + max(self.give_up_after, 60.0)
        wait = 2.0
        while True:
            try:
                return send()
            except (AuthError, RuntimeError):
                raise
            except OSError as e:
                if time.monotonic() > deadline:
                    raise
                log.warning("could not send %s (%s); retrying in %.0f s", what, e, wait)
                time.sleep(wait)
                wait = min(wait * 2, 20.0)

    def loop(self, once: bool = False) -> int:
        wait = self.poll
        announced = False
        lost_since = None
        while True:
            try:
                job = self.console.claim(self.name, self.kinds, self.info)
                wait, lost_since = self.poll, None
            except AuthError as e:
                print(f"{e}. Reconnect from the laptop (the token may have been changed in System).", file=sys.stderr)
                return 2
            except (OSError, RuntimeError) as e:
                if once:
                    print(f"cannot reach the console at {self.console.server}: {e} (is the SSH tunnel open?)",
                          file=sys.stderr)
                    return 3
                lost_since = lost_since or time.monotonic()
                if self.give_up_after and time.monotonic() - lost_since > self.give_up_after:
                    print(f"the console has been unreachable for {self.give_up_after:.0f} s: the SSH session "
                          "has closed; worker stopped", file=sys.stderr)
                    return 4
                log.warning("console unreachable (%s); retrying in %.0f s", e, wait)
                time.sleep(wait)
                wait = min(wait * 2, 30.0)
                continue
            if not announced:
                print(f"connected to {self.console.server} as '{self.name}' for {', '.join(self.kinds)}"
                      f" ({self.info['gpu'] or 'no GPU'}, {self.procs} processes)", flush=True)
                announced = True
            if job is None:
                if once:
                    return 0
                time.sleep(self.poll)
                continue
            try:
                self.run(job)
            except AuthError as e:
                print(f"{e}. Reconnect from the laptop.", file=sys.stderr)
                return 2
            if once:
                return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fa_console.worker",
                                 description="Run Future Agri console jobs on this machine (the GPU server).")
    ap.add_argument("--server", default=os.environ.get("FA_CONSOLE", DEFAULT_SERVER),
                    help=f"console URL as seen from here (default {DEFAULT_SERVER}, the SSH tunnel)")
    ap.add_argument("--token", default=os.environ.get("FA_WORKER_TOKEN"), help="worker token (prefer --token-file)")
    ap.add_argument("--token-file", help="file holding the token; read once and deleted")
    ap.add_argument("--name", default=socket.gethostname().split(".")[0], help="name shown in the console")
    ap.add_argument("--kinds", default=",".join(KINDS), help="job kinds to take (default: all this machine can run)")
    ap.add_argument("--home", default=os.environ.get("FA_WORKER_HOME", str(Path.home() / ".futureagri" / "worker")))
    ap.add_argument("--cache", default=os.environ.get("FA_DATA_CACHE"), help="data cache folder (weather, soil...)")
    ap.add_argument("--procs", type=int, default=default_procs(), help="CPU processes for iterations")
    ap.add_argument("--poll", type=float, default=3.0, help="seconds between polls when idle")
    ap.add_argument("--once", action="store_true", help="take at most one job, then exit")
    ap.add_argument("--give-up-after", type=float, default=0.0,
                    help="exit when the console has been unreachable this many seconds (0 = keep trying)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    token = a.token
    if a.token_file:
        tf = Path(a.token_file).expanduser()
        try:
            token = tf.read_text().strip()
        finally:
            tf.unlink(missing_ok=True)
    if not token:
        print("no worker token: pass --token-file (the connect script does this for you)", file=sys.stderr)
        return 2
    name = re.sub(r"[^A-Za-z0-9._-]", "-", a.name)[:64] or "gpu-worker"
    kinds = available_kinds(a.kinds.split(","))
    if not kinds:
        print(f"none of the job kinds '{a.kinds}' can run here", file=sys.stderr)
        return 2
    w = Worker(Console(a.server, token), name, kinds, Path(a.home).expanduser(),
               Path(a.cache).expanduser() if a.cache else None, max(1, a.procs), a.poll, a.give_up_after)
    try:
        return w.loop(once=a.once)
    except KeyboardInterrupt:
        print("worker stopped", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
