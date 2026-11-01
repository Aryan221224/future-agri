"""Persistent job queue with local worker threads and a remote-worker protocol.

A job function is ``fn(job: dict, ctx: JobContext) -> dict``. The returned dict
is saved whole to ``FA_HOME/jobs/<id>.json``; its ``summary`` (if any) is kept in
the database for listings. ``ctx.progress`` reports progress and is also where a
cancellation takes effect. Jobs survive restarts as records: a job that was
running when the process stopped is marked failed ("server restarted").

Remote workers (e.g. a GPU server reached over an SSH reverse tunnel) claim jobs
of kinds registered with ``remote=True`` using the worker token. A job submitted
with ``target="auto"`` goes to a remote worker that has polled within 60 s for
that kind, else to the local workers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
import traceback

from .store import TERMINAL, Store, now

log = logging.getLogger("fa_console.jobs")
REMOTE_ALIVE_S = 60.0
REMOTE_LOST_S = 600.0


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, mgr: "JobManager", job: dict):
        self.mgr, self.job = mgr, job

    @property
    def id(self) -> str:
        return self.job["id"]

    def check(self) -> None:
        if self.id in self.mgr._cancel:
            raise JobCancelled()

    def progress(self, frac: float, message: str = "") -> None:
        self.mgr.store.update_job(self.id, progress=round(max(0.0, min(1.0, float(frac))), 4),
                                  message=str(message)[:500], heartbeat_at=now())
        self.check()


class JobManager:
    def __init__(self, store: Store, settings):
        self.store, self.settings = store, settings
        self._fns: dict[str, callable] = {}
        self._remote_kinds: set[str] = set()
        self._cancel: set[str] = set()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.remote: dict[str, dict] = {}
        settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        for j in store._rows("SELECT id FROM jobs WHERE status='running'"):
            store.update_job(j["id"], status="failed", error="server restarted while the job was running; run it again",
                             finished_at=now())

    # ---------------------------------------------------------------- registration and submission
    def register(self, kind: str, fn, remote: bool = False) -> None:
        self._fns[kind] = fn
        if remote:
            self._remote_kinds.add(kind)

    def kinds(self) -> list[str]:
        return sorted(self._fns)

    def submit(self, kind: str, farm_id: str | None, params: dict, target: str = "auto") -> dict:
        if kind not in self._fns:
            raise ValueError(f"unknown job kind '{kind}'")
        if target not in ("auto", "local", "remote"):
            raise ValueError("target must be auto, local or remote")
        if target == "auto":
            target = "remote" if self._remote_alive(kind) else "local"
        if target == "remote" and kind not in self._remote_kinds:
            raise ValueError(f"'{kind}' jobs cannot run on a remote worker")
        job = self.store.create_job(kind, farm_id, params, target)
        self.store.add_event(farm_id, "job_submitted", {"job": job["id"], "kind": kind, "target": target})
        return job

    def _remote_alive(self, kind: str) -> bool:
        t = time.time()
        return kind in self._remote_kinds and any(
            kind in w["kinds"] and t - w["last_seen"] < REMOTE_ALIVE_S for w in self.remote.values())

    # ---------------------------------------------------------------- local execution
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for i in range(max(1, int(self.settings.local_workers))):
            t = threading.Thread(target=self._loop, args=(f"local-{i}",), daemon=True, name=f"fa-job-{i}")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        self._threads = []

    def _loop(self, name: str) -> None:
        while not self._stop.is_set():
            try:
                self._watchdog()
                job = self.store.claim_job(self.kinds(), name, ("local",))
            except Exception:  # noqa: BLE001 -- keep the worker alive
                log.exception("job claim failed")
                job = None
            if job is None:
                self._stop.wait(0.25)
                continue
            self._run(job)

    def run_pending(self, max_jobs: int | None = None) -> int:
        """Run queued local jobs in this thread (tests, single-process tools)."""
        n = 0
        while max_jobs is None or n < max_jobs:
            job = self.store.claim_job(self.kinds(), "inline", ("local",))
            if job is None:
                break
            self._run(job)
            n += 1
        return n

    def _run(self, job: dict) -> None:
        jid = job["id"]
        ctx = JobContext(self, job)
        try:
            ctx.check()
            result = self._fns[job["kind"]](job, ctx) or {}
            self._finish(jid, result)
        except JobCancelled:
            self.store.update_job(jid, status="cancelled", finished_at=now(), message="cancelled by the operator")
        except Exception as e:  # noqa: BLE001 -- a failed job is a record, not a crash
            log.error("job %s failed\n%s", jid, traceback.format_exc())
            self.store.update_job(jid, status="failed", error=f"{type(e).__name__}: {e}"[:2000], finished_at=now())
        finally:
            self._cancel.discard(jid)

    def _finish(self, jid: str, result: dict) -> None:
        path = self.settings.jobs_dir / f"{jid}.json"
        path.write_text(json.dumps(result, default=_json_default))
        summary = result.get("summary") if isinstance(result, dict) else None
        self.store.update_job(jid, status="done", progress=1.0, result=summary, finished_at=now())
        job = self.store.get_job(jid)
        self.store.add_event(job["farm_id"], "job_done", {"job": jid, "kind": job["kind"]})

    def result(self, jid: str) -> dict | None:
        p = self.settings.jobs_dir / f"{jid}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def cancel(self, jid: str) -> dict:
        job = self.store.get_job(jid)
        if job is None:
            raise KeyError(jid)
        if job["status"] == "queued":
            self.store.update_job(jid, status="cancelled", finished_at=now(), message="cancelled before it started")
        elif job["status"] == "running":
            self._cancel.add(jid)
        return self.store.get_job(jid)

    def _watchdog(self) -> None:
        import datetime as dt
        t = time.time()
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=REMOTE_LOST_S)).isoformat()
        for j in self.store._rows("SELECT id FROM jobs WHERE status='running' AND target='remote' "
                                  "AND heartbeat_at < ?", (cutoff,)):
            self.store.update_job(j["id"], status="failed", finished_at=now(),
                                  error=f"GPU worker lost: no progress for {REMOTE_LOST_S / 60:.0f} min")
        if any(t - w["last_seen"] < REMOTE_ALIVE_S for w in self.remote.values()):
            return
        for j in self.store._rows("SELECT id, kind FROM jobs WHERE status='queued' AND target='remote'"):
            # no remote worker alive: run it here instead of waiting forever
            self.store.update_job(j["id"], target="local", message="no GPU worker connected; running locally")

    # ---------------------------------------------------------------- events (SSE)
    async def events(self, jid: str, interval: float = 0.4):
        last = None
        while True:
            job = self.store.get_job(jid)
            if job is None:
                return
            snap = (job["status"], job["progress"], job["message"])
            if snap != last:
                last = snap
                yield {k: job[k] for k in ("id", "kind", "status", "progress", "message", "error", "result", "worker")}
            if job["status"] in TERMINAL:
                return
            await asyncio.sleep(interval)

    # ---------------------------------------------------------------- remote workers
    def worker_token(self) -> str:
        tok = self.store.get_setting("worker_token")
        if not tok:
            tok = secrets.token_urlsafe(24)
            self.store.set_setting("worker_token", tok)
        return tok

    def rotate_worker_token(self) -> str:
        tok = secrets.token_urlsafe(24)
        self.store.set_setting("worker_token", tok)
        return tok

    def _auth(self, token: str) -> None:
        if not token or not secrets.compare_digest(str(token), self.worker_token()):
            raise PermissionError("bad worker token")

    def claim(self, token: str, worker: str, kinds: list[str], info: dict | None = None) -> dict | None:
        self._auth(token)
        ok = [k for k in kinds if k in self._remote_kinds]
        self.remote[worker] = {"name": worker, "kinds": ok, "last_seen": time.time(), "info": info or {}}
        job = self.store.claim_job(ok, worker, ("remote",))
        if job:
            self.store.add_event(job["farm_id"], "job_remote", {"job": job["id"], "worker": worker})
        return job

    def report(self, token: str, jid: str, progress: float, message: str = "") -> dict:
        self._auth(token)
        job = self.store.get_job(jid)
        if job is None or job["status"] != "running":
            raise KeyError(jid)
        if job["worker"] in self.remote:
            self.remote[job["worker"]]["last_seen"] = time.time()
        self.store.update_job(jid, progress=round(float(progress), 4), message=str(message)[:500], heartbeat_at=now())
        return {"cancel": jid in self._cancel}

    def complete(self, token: str, jid: str, result: dict | None = None, error: str | None = None) -> dict:
        self._auth(token)
        job = self.store.get_job(jid)
        if job is None or job["status"] != "running":
            raise KeyError(jid)
        if error:
            self.store.update_job(jid, status="failed", error=str(error)[:2000], finished_at=now())
        else:
            self._finish(jid, result or {})
        self._cancel.discard(jid)
        return self.store.get_job(jid)

    def workers(self) -> dict:
        t = time.time()
        return {"local": len(self._threads),
                "remote": [{"name": w["name"], "kinds": w["kinds"], "seen_s_ago": round(t - w["last_seen"], 1),
                            "alive": t - w["last_seen"] < REMOTE_ALIVE_S, "info": w["info"]}
                           for w in self.remote.values()]}


def _json_default(o):
    import datetime as dt
    import numpy as np
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.generic):
        return o.item()
    if hasattr(o, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(o)
    return str(o)
