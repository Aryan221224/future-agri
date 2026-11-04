"""The console's side of flying the real drone.

* ``AgentHub``: the K3-A Pro's on-board agent (fa-agent). The agent dials out (works through
  4G NAT): it long-polls for commands and posts its state; the operator's commands queue
  here. Starting a job also opens an aircraft live run, so the agent's telemetry and camera
  counts drive the Live run page.
* ``ArdupilotService``: an ArduPilot flight controller over MAVLink (USB, SiK radio, Wi-Fi,
  4G): connect, preflight, fly the job sortie by sortie (fa_aircraft.JobRunner), abort, with
  its telemetry fed into the same live run.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from .aircraft_jobs import build_job, job_distance_ok
from .devices import token_hash
from .errors import Busy, NotFound
from .store import new_id, now

AGENT_COMMANDS = {"start_job", "calibrate", "abort", "pause", "resume", "battery_swapped"}


class AgentHub:
    def __init__(self, store, farms, sessions, live_run):
        self.store, self.farms, self.sessions, self.live_run = store, farms, sessions, live_run
        self._cv = threading.Condition()
        self._queue: dict[str, deque] = {}
        self._state: dict[str, dict] = {}
        self._acks: dict[str, dict] = {}

    def _device(self, token: str) -> dict:
        dev = self.store.device_by_token_hash(token_hash(token or ""))
        if dev is None or dev.get("kind") != "drone_agent":
            raise PermissionError("unknown drone-agent token")
        return dev

    # ---------------------------------------------------------------- the agent's side
    def poll(self, token: str, wait_s: float = 20.0) -> dict:
        fid = self._device(token)["farm_id"]
        end = time.monotonic() + max(0.0, min(wait_s, 30.0))
        with self._cv:
            q = self._queue.setdefault(fid, deque())
            while not q and time.monotonic() < end:
                self._cv.wait(end - time.monotonic())
            out = list(q)
            q.clear()
        return {"commands": out}

    def post_state(self, token: str, body: dict) -> dict:
        dev = self._device(token)
        fid = dev["farm_id"]
        prev = self._state.get(fid, {}).get("state", {}).get("state")
        self._state[fid] = {"at": time.time(), "state": body}
        if body.get("state") != prev:
            self.store.add_event(fid, "agent_state", {"state": body.get("state"),
                                                      "reason": (body.get("events") or [[None, None, ""]])[-1][2]})
        self.store.touch_device(dev["id"])
        return {"ok": True}

    def ack(self, token: str, body: dict) -> dict:
        fid = self._device(token)["farm_id"]
        self._acks[str(body.get("id"))] = {**body, "farm": fid, "at": time.time()}
        return {"ok": True}

    # ---------------------------------------------------------------- the operator's side
    def command(self, fid: str, kind: str, op_id: str | None = None, home: dict | None = None) -> dict:
        if kind not in AGENT_COMMANDS:
            raise ValueError(f"command must be one of {sorted(AGENT_COMMANDS)}")
        self.farms.get(fid)
        cmd = {"id": new_id("cmd_"), "kind": kind, "at": now()}
        if kind in ("start_job", "calibrate"):
            if not op_id:
                raise ValueError(f"{kind} needs op_id (the job whose field and settings to use)")
            job = build_job(self.farms, self.sessions, fid, op_id, home=home)
            job_distance_ok(job)
            cmd["job"] = job
            if kind == "start_job":
                run = self.live_run.start(fid, op_id, source="aircraft")
                cmd["run_id"] = run["run"]["run_id"]
        with self._cv:
            self._queue.setdefault(fid, deque()).append(cmd)
            self._cv.notify_all()
        self.store.add_event(fid, "agent_command", {"id": cmd["id"], "kind": kind})
        return {"command": {k: v for k, v in cmd.items() if k != "job"},
                "job_summary": cmd.get("job", {}).get("summary")}

    def view(self, fid: str) -> dict:
        st = self._state.get(fid)
        pend = list(self._queue.get(fid, ()))
        acks = [a for a in self._acks.values() if a["farm"] == fid][-10:]
        return {"online": bool(st and time.time() - st["at"] < 5.0),
                "seen_s_ago": None if not st else round(time.time() - st["at"], 1),
                "agent": st["state"] if st else None, "pending": [c["kind"] for c in pend], "acks": acks}


class ArdupilotService:
    def __init__(self, store, farms, sessions, live_run):
        self.store, self.farms, self.sessions, self.live_run = store, farms, sessions, live_run
        self._v: dict[str, object] = {}
        self._run: dict[str, dict] = {}
        self._lock = threading.Lock()

    def connect(self, fid: str, link: str, timeout_s: float = 30.0) -> dict:
        import fa_aircraft  # noqa: F401  (MAVLink 2)
        from fa_aircraft.vehicle import Vehicle, VehicleError
        self.farms.get(fid)
        old = self._v.pop(fid, None)
        if old is not None:
            old.close()
        try:
            self._v[fid] = Vehicle(link, timeout_s=timeout_s)
        except VehicleError as e:
            raise ValueError(str(e)) from e
        self.store.add_event(fid, "aircraft_connected", {"link": link})
        return self.status(fid)

    def _vehicle(self, fid: str):
        v = self._v.get(fid)
        if v is None:
            raise NotFound("no flight controller connected on this farm")
        return v

    def disconnect(self, fid: str) -> dict:
        r = self._run.get(fid)
        if r and r["thread"].is_alive():
            raise Busy("a job is flying: abort it first")
        v = self._v.pop(fid, None)
        if v is not None:
            v.close()
        return {"connected": False}

    def preflight(self, fid: str) -> dict:
        from fa_aircraft.runner import SAFETY_PARAMS, load_param_file, preflight
        rep = preflight(self._vehicle(fid), expected=load_param_file(keys=SAFETY_PARAMS))
        return {"ok": rep.ok, "failures": rep.failures, "warnings": rep.warnings}

    def start(self, fid: str, op_id: str, home: dict | None = None) -> dict:
        from fa_agent.route import Job
        from fa_aircraft.runner import JobRunner
        v = self._vehicle(fid)
        with self._lock:
            r = self._run.get(fid)
            if r and r["thread"].is_alive():
                raise Busy("a job is already flying on this farm")
            if home is None and v.state.lat is not None:
                home = {"lat": v.state.lat, "lon": v.state.lon}          # where the aircraft stands
            jd = build_job(self.farms, self.sessions, fid, op_id, home=home)
            job_distance_ok(jd)
            job = Job.from_json(jd)
            run = self.live_run.start(fid, op_id, source="aircraft")["run"]
            runner = JobRunner(v, job, on_event=lambda e: self.store.add_event(
                fid, "aircraft_event", {"state": e.state, "reason": e.reason}))
            th = threading.Thread(target=runner.run, daemon=True, name=f"job {fid}")
            self._run[fid] = {"runner": runner, "thread": th, "run_id": run["run_id"], "job": jd["summary"],
                              "local": job.local}
            th.start()
            threading.Thread(target=self._telemetry, args=(fid,), daemon=True, name=f"telemetry {fid}").start()
        return self.status(fid)

    def _telemetry(self, fid: str) -> None:
        """The aircraft's position into the live run, once a second, while the job runs."""
        r, seq = self._run[fid], 0
        v = self._v.get(fid)
        while r["thread"].is_alive() and v is not None:
            s = v.state
            if s.lat is not None:
                seq += 1
                batch = {"schema": "fa.live/1", "run_id": r["run_id"], "seq": seq, "sent_at": now(),
                         "cells": [], "telemetry": [{
                             "t": now(), "lat": s.lat, "lon": s.lon, "alt_agl_m": max(-1.0, s.rel_alt or 0.0),
                             "heading_deg": s.heading, "speed_ms": min(60.0, (s.vx ** 2 + s.vy ** 2) ** 0.5),
                             "spraying": r["runner"].state == "AUTO" and (s.rel_alt or 0) < 6.0,
                             "sortie": r["runner"].sortie + 1}]}
                try:
                    self.live_run.ingest_for(fid, batch)
                except Exception:  # noqa: BLE001 -- a rejected point (e.g. far off the field) is skipped
                    pass
            time.sleep(1.0)

    def battery_swapped(self, fid: str) -> dict:
        r = self._run.get(fid)
        if not r:
            raise NotFound("no job on this farm")
        r["runner"].battery_swapped()
        return self.status(fid)

    def abort(self, fid: str) -> dict:
        r = self._run.get(fid)
        if r and r["thread"].is_alive():
            r["runner"].abort()
        else:
            from fa_aircraft.vehicle import VehicleError
            try:
                self._vehicle(fid).set_mode("RTL")
            except VehicleError as e:
                raise ValueError(f"RTL not accepted: {e}") from e
        return self.status(fid)

    def status(self, fid: str) -> dict:
        v = self._v.get(fid)
        r = self._run.get(fid)
        out = {"connected": v is not None, "link": getattr(v, "link", None),
               "link_ok": bool(v and v.link_ok()), "vehicle": v.state.as_dict() if v else None}
        if r:
            ru = r["runner"]
            out["job"] = {"state": ru.state, "sortie": ru.sortie + 1, "summary": r["job"], "run_id": r["run_id"],
                          "flying": r["thread"].is_alive(),
                          "events": [{"t": e.t, "state": e.state, "reason": e.reason} for e in ru.events[-30:]],
                          "preflight": None if ru.preflight is None else {"ok": ru.preflight.ok,
                                                                          "failures": ru.preflight.failures,
                                                                          "warnings": ru.preflight.warnings}}
        return out
