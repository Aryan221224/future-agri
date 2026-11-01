"""Mission sessions: one interactive ``SeasonSession`` per farm.

Every action is persisted as an event (kind ``session_action``) and replayed when
the server restarts, so the session is durable without pickling crop models.
Actions on a farm are serialised by a per-farm lock; a second action while one
runs gets HTTP 409. Reads come from a view published after each action, so they
never wait for a long run.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import threading
from dataclasses import asdict

import numpy as np

from fa_drone.mission import MissionSettings
from fa_farm.sources.base import DataUnavailable
from fa_farm.sources.forecasts import ForecastSource
from fa_runner.scenarios import ForecastFeed
from fa_runner.session import SeasonSession
from fa_runner.world import RealWorld
from fa_scan.importer import import_scan

from .errors import Busy, NotFound
from .scene import b64, farm_grid
from .store import new_id

ACTIONS = {"step", "run_until", "approve", "reject", "defer", "fly_now", "scan_now", "record_flight",
           "report_scouting", "fly_closed_loop"}
SNAP_VARS = ("lai", "cc", "weeds", "blight", "dose_fungicide", "dose_herbicide")


class SessionService:
    def __init__(self, store, settings, farms, jobs):
        self.store, self.settings, self.farms, self.jobs = store, settings, farms, jobs
        self.defaults = {"policy": "twin", "plan_members": 10, "scan_das": 45, "scan_gsd_m": 0.005, "seed": 0}
        self._sessions: dict[str, SeasonSession] = {}
        self._views: dict[str, dict] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._glock = threading.Lock()
        jobs.register("session_run", self.run_job)

    # ---------------------------------------------------------------- construction
    def _lock(self, fid: str) -> threading.Lock:
        with self._glock:
            return self._locks.setdefault(fid, threading.Lock())

    def config(self, fid: str) -> dict:
        cfg = dict(self.defaults)
        cfg["flight"] = asdict(MissionSettings())
        cfg["plan_id"] = None
        plan = self.store.approved_plan(fid)
        if plan:
            p = plan["payload"]
            cfg.update(policy=p["policy"], flight=p["flight"], scan_das=p["scan"]["das"],
                       scan_gsd_m=p["scan"]["gsd_m"], plan_id=plan["id"], plan_name=p.get("plan"))
        return cfg

    def _extra_weather_path(self, fid: str):
        return self.store.farm_dir(fid) / "weather_extra.json"

    def _extra_weather(self, fid: str) -> list[dict]:
        p = self._extra_weather_path(fid)
        if not p.exists():
            return []
        return [dict(r, DAY=dt.date.fromisoformat(r["DAY"])) for r in json.loads(p.read_text())]

    def _kwargs(self, fid: str) -> dict:
        farm = self.farms.get(fid)
        vf = self.farms.load(fid)
        cfg = self.config(fid)
        lat, lon = vf.spec.centroid
        sources = [ForecastSource.CLIMATOLOGY] if self.settings.offline else None
        feed = ForecastFeed(lat, lon, vf.history, self.settings.data_cache(), sources)
        world = RealWorld(vf, extra_weather=self._extra_weather(fid)) if farm["mode"] == "field" else None
        return dict(vf=vf, policy=cfg["policy"], seed=int(cfg["seed"]), feed=feed,
                    flight=MissionSettings(**cfg["flight"]), scan_das=int(cfg["scan_das"]),
                    scan_gsd_m=float(cfg["scan_gsd_m"]), plan_members=int(cfg["plan_members"]),
                    approvals="manual", world=world, keep_tracks=True)

    def _load_scan(self, fid: str, ref: str):
        p = self.store.farm_dir(fid) / "scans" / f"{ref}.json"
        d = json.loads(p.read_text())
        grid = farm_grid(self.farms.load(fid))
        if d.get("live_run"):                     # what a live run's camera saw (partial scan)
            from .live_run import obs_from_saved
            return obs_from_saved(d, grid)
        cover = np.asarray(d["cover"]) if d.get("cover") is not None else None
        obs, _ = import_scan(d["geojson"], grid, d["gsd_m"], d["day_index"], cover_cells=cover, dvs=d.get("dvs"))
        return obs

    def get(self, fid: str) -> SeasonSession:
        s = self._sessions.get(fid)
        if s is None:
            actions = [e["payload"] for e in self.store.events(fid, kinds=["session_action"], limit=1_000_000)]
            kw = self._kwargs(fid)
            try:
                s = (SeasonSession.restore(kw, actions, resolver=lambda ref: self._load_scan(fid, ref))
                     if actions else SeasonSession(**kw))
            except Exception as e:  # noqa: BLE001 -- e.g. a live field whose season has not started
                raise ValueError(f"the mission session cannot start yet: {e}") from e
            self._sessions[fid] = s
            self._publish(fid, s)
        return s

    # ---------------------------------------------------------------- views
    def _publish(self, fid: str, s: SeasonSession) -> None:
        farm = self.store.get_farm(fid)
        tracks = self.store.farm_dir(fid) / "tracks"
        if s.flight_tracks:
            tracks.mkdir(exist_ok=True)
            for oid, tr in list(s.flight_tracks.items()):
                (tracks / f"{oid}.json").write_text(json.dumps({"op": oid, "track": tr}))
            s.flight_tracks.clear()
        state = s.state()
        self.store.put_operations(fid, state["ops"])
        w = s.world
        waiting = None
        if not s.done and s.phase == "morning" and w.weather(s.day - dt.timedelta(days=1)) is None:
            waiting = (f"waiting for observed weather of {s.day - dt.timedelta(days=1)} "
                       "(NASA POWER publishes with a few days' lag)")
        cfg = self.config(fid)
        state.update(recent_events=s.events[-40:], mode=farm["mode"] if farm else None,
                     world={"has_truth": bool(getattr(w, "has_truth", True)),
                            "last_observed": w.last_observed().isoformat(), "waiting": waiting},
                     plan={"id": cfg["plan_id"], "name": cfg.get("plan_name"), "policy": cfg["policy"],
                           "flight": cfg["flight"], "scan_das": cfg["scan_das"], "scan_gsd_m": cfg["scan_gsd_m"]},
                     twin=self._twin_view(s))
        self._views[fid] = {"state": state, "trace": list(s.trace), "events": list(s.events),
                            "snapshots": dict(s.snapshots)}

    @staticmethod
    def _twin_view(s: SeasonSession) -> dict | None:
        if s.twin is None:
            return None
        row = next((r for r in reversed(s.trace) if r.get("twin_lai") is not None), None)
        return {"lai": row and row["twin_lai"], "dvs": row and row["twin_dvs"], "twso": row and row["twin_twso"],
                "herbicide_option": s.st["herb"], "scans": s.st["scans"]}

    def _view(self, fid: str) -> dict:
        v = self._views.get(fid)
        if v is None:
            lock = self._lock(fid)
            if not lock.acquire(timeout=60):
                raise Busy("session busy: it is still loading")
            try:
                if fid not in self._views:
                    self.get(fid)
            finally:
                lock.release()
            v = self._views[fid]
        return v

    def state(self, fid: str) -> dict:
        self.farms.get(fid)
        return self._view(fid)["state"]

    def trace(self, fid: str) -> dict:
        v = self._view(fid)
        return {"trace": v["trace"], "events": v["events"]}

    def snapshots(self, fid: str, day: str | None = None) -> dict:
        snaps = self._view(fid)["snapshots"]
        days = sorted(snaps)
        pick = [day] if day else days
        if day and day not in snaps:
            raise NotFound(f"no snapshot for {day}")
        return {"days": days, "data": {d: {f"{k}_b64": b64(snaps[d][k], "<f2") for k in SNAP_VARS if k in snaps[d]}
                                       for d in pick}}

    def track(self, fid: str, op_id: str) -> dict:
        p = self.store.farm_dir(fid) / "tracks" / f"{op_id}.json"
        if not p.exists():
            raise NotFound(f"no recorded track for {op_id}")
        return json.loads(p.read_text())

    # ---------------------------------------------------------------- actions
    def _call(self, fid: str, s: SeasonSession, action: str, params: dict):
        n0 = len(s.actions)
        try:
            return getattr(s, action)(**params)
        finally:
            for a in s.actions[n0:]:
                self.store.add_event(fid, "session_action", a)
            self._publish(fid, s)

    def act(self, fid: str, action: str, params: dict | None = None) -> dict:
        if action not in ACTIONS:
            raise ValueError(f"unknown session action '{action}'")
        self.farms.get(fid)
        lock = self._lock(fid)
        if not lock.acquire(blocking=False):
            raise Busy("session busy: another action is running on this farm")
        try:
            s = self.get(fid)
            out = self._call(fid, s, action, params or {})
            return {"result": out, "state": self._views[fid]["state"]}
        finally:
            lock.release()

    def run_job(self, job: dict, ctx) -> dict:
        fid, p = job["farm_id"], job["params"]
        lock = self._lock(fid)
        if not lock.acquire(blocking=False):
            raise Busy("session busy: another action is running on this farm")
        try:
            s = self.get(fid)
            total = max(1, (s.world.harvest - s.world.sowing).days)
            until = p.get("until")
            until_d = dt.date.fromisoformat(until) if until else None
            while not s.done:
                ev = self._call(fid, s, "run_until", {"day": until, "max_days": 7,
                                                      "stop_on_decision": bool(p.get("stop_on_decision", True)),
                                                      "approve_all": bool(p.get("approve_all", False))})
                das = (s.day - s.world.sowing).days
                ctx.progress(min(1.0, das / total), f"{s.day} - day {das} of the season")
                if s.phase == "flights" or (ev and ev[-1]["kind"] == "waiting"):
                    break
                if until_d is not None and s.day > until_d:
                    break
            return {"summary": {"day": s.day.isoformat(), "phase": s.phase, "done": s.done}}
        finally:
            lock.release()

    def reset(self, fid: str) -> dict:
        self.farms.get(fid)
        lock = self._lock(fid)
        if not lock.acquire(blocking=False):
            raise Busy("session busy: wait for the running action to finish")
        try:
            self._sessions.pop(fid, None)
            self._views.pop(fid, None)
            self.store.delete_events(fid, ["session_action"])
            self.store.put_operations(fid, [])
            shutil.rmtree(self.store.farm_dir(fid) / "tracks", ignore_errors=True)
            self.store.add_event(fid, "session_reset", {})
        finally:
            lock.release()
        return self.state(fid)

    def finish(self, fid: str) -> dict:
        lock = self._lock(fid)
        if not lock.acquire(blocking=False):
            raise Busy("session busy")
        try:
            res = self.get(fid).finish()
        finally:
            lock.release()
        out = {**res.summary(), "missions_log": res.missions_log, "decisions": res.decisions}
        (self.store.farm_dir(fid) / "season_result.json").write_text(json.dumps(out, default=str))
        self.store.add_event(fid, "season_finished", {"net_ha": res.net_ha, "yield": res.yield_dry_kg_ha})
        return out

    def upload_scan(self, fid: str, geojson: dict, gsd_m: float) -> dict:
        if not 0.001 <= gsd_m <= 0.1:
            raise ValueError("ground sampling distance must be 1 mm to 10 cm")
        lock = self._lock(fid)
        if not lock.acquire(blocking=False):
            raise Busy("session busy: another action is running on this farm")
        try:
            s = self.get(fid)
            if s.done:
                raise ValueError("the season is finished")
            das = (s.day - s.world.sowing).days
            cover = self._cover_hint(fid, s)
            dvs = s.trace[-1]["twin_dvs"] if s.trace and s.trace[-1].get("twin_dvs") is not None else None
            obs, report = import_scan(geojson, s.grid, gsd_m, das, cover_cells=cover, dvs=dvs)
            ref = new_id("scan_")
            d = self.store.farm_dir(fid) / "scans"
            d.mkdir(exist_ok=True)
            (d / f"{ref}.json").write_text(json.dumps({"geojson": geojson, "gsd_m": gsd_m, "day_index": das,
                                                       "cover": None if cover is None else cover.tolist(),
                                                       "dvs": dvs}))
            self._call(fid, s, "scan_now", {"obs": obs, "ref": ref})
        finally:
            lock.release()
        self.store.add_event(fid, "scan_uploaded", {"ref": ref, **{k: v for k, v in report.items() if k != "errors"}})
        return {"report": report, "ref": ref, "state": self._views[fid]["state"]}

    def _cover_hint(self, fid: str, s: SeasonSession):
        """Field-level Sentinel-2 cover near today, broadcast over the cells (live fields)."""
        vf = self.farms.load(fid)
        ser = [x for x in getattr(vf, "s2_series", []) if x[0] <= s.day]
        if getattr(s.world, "has_truth", True) or not ser:
            return None
        d, _, cov = ser[-1]
        if (s.day - d).days > 10:
            return None
        return np.full(s.grid.n_cells, float(cov))

    def record_flight(self, fid: str, op_id: str, record: dict) -> dict:
        out = self.act(fid, "record_flight", {"op_id": op_id, "record": record})
        op = out["result"]
        self.store.add_record(fid, {**record, "operation_id": op_id, "product": op["product"],
                                    "area_ha": op["result"].get("area_ha")}, source="aircraft")
        return out

    def refresh_weather(self, fid: str) -> dict:
        """Fetch newly published NASA POWER days for a live field."""
        farm = self.farms.get(fid)
        if farm["mode"] != "field":
            return {"added": 0, "note": "virtual farms use their recorded season"}
        if self.settings.offline:
            return {"added": 0, "note": "offline"}
        from fa_farm.sources.weather import power_recent
        lock = self._lock(fid)
        if not lock.acquire(blocking=False):
            raise Busy("session busy")
        try:
            s = self.get(fid)
            last = s.world.last_observed()
            try:
                recs, prov, _ = power_recent(farm["lat"], farm["lon"], last + dt.timedelta(days=1),
                                             self.settings.data_cache())
            except DataUnavailable as e:
                return {"added": 0, "note": str(e)}
            added = s.world.extend(recs)
            if added:
                p = self._extra_weather_path(fid)
                old = json.loads(p.read_text()) if p.exists() else []
                old += [dict(r, DAY=r["DAY"].isoformat()) for r in recs if r["DAY"] > last][:added]
                p.write_text(json.dumps(old))
                self._publish(fid, s)
            return {"added": added, "last_observed": s.world.last_observed().isoformat(), "note": prov.note}
        finally:
            lock.release()
