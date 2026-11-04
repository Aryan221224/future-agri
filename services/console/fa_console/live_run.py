"""Live run: one spray job flown as a closed loop, as the console sees it.

Two sources:

* **simulated camera** (virtual farms): a job runs ``fly_closed_loop`` sortie by sortie and
  each sortie report is published here as it happens;
* **aircraft**: the on-board agent posts message batches (schema fa.live/1) with a device
  token. Each new (run_id, seq) is counted once; the cells seen update the weed map, the
  deviation from the frozen prediction is scored, and the remaining targets are re-planned
  (unless the operator froze the plan). The next sortie is a KML for the aircraft app;
  nothing is ever sent to the aircraft. ``finish`` hands what was seen to the twin as a
  partial scan.

Dose from telemetry is approximate: a cell passed by a spraying segment within half a swath
gets the prescribed rate once per sortie (flow is not yet used). The last point of each sortie
is kept between batches, so a pass split over several batches loses no segment. Telemetry
far outside the field is rejected item by item.

A run in progress is never replaced (409); a batch is counted only once it has been applied,
so a batch that failed can be sent again; ``finish`` books the job only once the session
accepted it, so a busy session leaves the run running and the call can be retried.
"""
from __future__ import annotations

import copy
import json
import threading

import numpy as np

from fa_core.live_msgs import validate_batch
from fa_runner.closedloop.prediction import deviation_dose, deviation_weeds, freeze_prediction
from fa_runner.closedloop.replan import merge_observations, remaining_targets
from fa_scan.measure import ScanObservation
from fa_scan.sprayer_camera import SprayerCamera, cells_along, estimate_from_counts

from .devices import token_hash
from .errors import Busy, NotFound
from .scene import farm_grid
from .store import new_id, now

MAX_OFF_FIELD_M = 150.0      # telemetry this far outside the field boundary is not this field's
_ARR = ("weeds", "weeds_sd", "seen", "dose", "targets", "orig_targets", "pred_mean", "pred_sd", "pred_dose")


def obs_from_saved(d: dict, grid) -> ScanObservation:
    """The partial scan a finished live run stored (replayed on session restore)."""
    n = grid.n_cells
    seen = np.zeros(n, bool)
    seen[d["cells"]] = True
    est = np.full(n, np.nan)
    sd = np.full(n, np.inf)
    est[d["cells"]] = d["est"]
    sd[d["cells"]] = [np.inf if v is None else v for v in d["sd"]]
    return ScanObservation(int(d["day_index"]), float(d["gsd_m"]), np.full(n, 0.5), 10.0, np.full(n, 1.0), 10.0,
                           np.zeros(n), est, sd, 0.0, np.zeros(n, bool), 0.0,
                           ["live run: the sprayer camera's per-cell counts; cover and stage not observed"],
                           observed=seen, crop_observed=False)


class LiveRunService:
    def __init__(self, store, farms, sessions, jobs, exports):
        self.store, self.farms, self.sessions, self.jobs, self.exports = store, farms, sessions, jobs, exports
        self._runs: dict[str, dict] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        jobs.register("live_run", self._job)

    def _flock(self, fid: str) -> threading.Lock:
        """One lock per farm: a slow farm never holds up another farm's aircraft."""
        with self._guard:
            return self._locks.setdefault(fid, threading.Lock())

    # ---------------------------------------------------------------- persistence
    def _path(self, fid: str):
        d = self.store.farm_dir(fid) / "live_run"
        d.mkdir(exist_ok=True)
        return d / "current.json"

    def _save(self, fid: str, r: dict) -> None:
        out = {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in r.items()}
        out = {k: ([None if isinstance(x, float) and not np.isfinite(x) else x for x in v]
                   if k in _ARR else v) for k, v in out.items()}
        self._path(fid).write_text(json.dumps(out))

    def _get(self, fid: str) -> dict:
        r = self._runs.get(fid)
        if r is None:
            p = self._path(fid)
            if not p.exists():
                raise NotFound("no live run on this farm")
            r = json.loads(p.read_text())
            for k in _ARR:
                if k in r and r[k] is not None:
                    dtype = bool if k in ("seen", "targets", "orig_targets") else float
                    fill = np.inf if k.endswith("_sd") else np.nan
                    r[k] = np.array([fill if v is None else v for v in r[k]], dtype=dtype)
            self._runs[fid] = r
        return r

    # ---------------------------------------------------------------- start
    def start(self, fid: str, op_id: str, valve: bool = False, source: str | None = None) -> dict:
        farm = self.farms.get(fid)
        source = source or ("aircraft" if farm["mode"] == "field" else "simulated")
        if source not in ("simulated", "aircraft"):
            raise ValueError("source must be 'simulated' or 'aircraft'")
        try:
            cur = self._get(fid)
        except NotFound:
            cur = None
        if cur is not None and cur["status"] == "running":
            raise Busy(f"live run {cur['run_id']} is running on this farm: finish it before starting another")
        s = self.sessions.get(fid)
        op = s.ops.get(op_id)
        if op is None:
            raise NotFound(f"no operation {op_id}")
        if op.status not in ("proposed", "approved") or op.product not in ("herbicide", "fungicide"):
            raise ValueError(f"operation {op_id} is a {op.product} job, {op.status}: nothing to fly")
        if source == "simulated":
            if farm["mode"] == "field":
                raise ValueError("a live field has no simulated camera: start a live run from the aircraft")
            r = {"run_id": new_id("run_"), "source": "simulated camera", "op_id": op_id, "product": op.product,
                 "mode": "closed loop", "status": "running", "valve": bool(valve), "started": now(),
                 "sorties": [], "audit": [], "planned": None, "flown": None, "batches": 0, "seqs": []}
            with self._flock(fid):
                self._runs[fid] = r
                self._save(fid, r)
            job = self.jobs.submit("live_run", fid, {"op_id": op_id, "valve": bool(valve)})
            return {"job": job, "run": self.state(fid)}
        pred = freeze_prediction(s, op)
        flight = s.flight if not op.settings else type(s.flight)(**op.settings)
        r = {"run_id": new_id("run_"), "source": "aircraft", "op_id": op_id, "product": op.product,
             "mode": "closed loop", "status": "running", "valve": False, "started": now(), "sorties": [],
             "audit": [], "batches": 0, "seqs": [], "day": s.day.isoformat(),
             "das": (s.day - s.world.sowing).days, "rate": flight.rate_l_ha, "swath": flight.swath_m,
             "height": flight.height_m, "gsd_m": None,
             "planned": {"litres": round(pred.litres, 2), "sorties": pred.sorties, "time_min": round(pred.time_min, 1)},
             "flown": {"litres": 0.0, "sorties": 0, "time_min": 0.0}, "drone": None, "detections": [],
             "weeds": pred.weeds_mean.copy(), "weeds_sd": pred.weeds_sd.copy(),
             "seen": np.zeros(len(pred.targets), bool), "dose": np.zeros(len(pred.targets)),
             "targets": pred.targets.copy(), "orig_targets": pred.targets.copy(),
             "pred_mean": pred.weeds_mean, "pred_sd": pred.weeds_sd, "pred_dose": pred.dose_l_ha,
             "deviation": {}, "last_tel": {}, "sortie_cells": {}}
        with self._flock(fid):
            self._runs[fid] = r
            self._save(fid, r)
        self.store.add_event(fid, "live_run_started", {"run": r["run_id"], "op": op_id, "source": "aircraft"})
        return {"run": self.state(fid)}

    # ---------------------------------------------------------------- simulated camera
    def _job(self, job: dict, ctx) -> dict:
        fid, p = job["farm_id"], job["params"]
        r = self._get(fid)
        s = self.sessions.get(fid)

        def on_sortie(rep: dict) -> None:
            with self._flock(fid):
                r["sorties"].append({k: rep[k] for k in ("n", "cells_sprayed", "cells_seen", "litres", "time_min",
                                                         "correction", "remaining")})
                r["drone"] = rep["track"][-1] if rep["track"] else r.get("drone")
                r["track"] = rep["track"]
                r["detections"] = rep["detections"]
                r["remaining"] = rep["targets_next"]
                r["seen_cells"] = sorted(set(r.get("seen_cells", [])) | set(rep["seen_cells"]))
                r["deviation"] = {d["channel"]: d["cells"] for d in rep["deviations"] if d["significant"]}
                self._save(fid, r)
            ctx.progress(min(0.95, 0.1 + 0.05 * rep["n"]), f"sortie {rep['n']}: {rep['correction']}")
        s.closed_loop_listener = on_sortie
        try:
            out = self.sessions.act(fid, "fly_closed_loop", {"op_id": p["op_id"], "valve": p["valve"]})["result"]
        except Exception as e:
            with self._flock(fid):
                r.update(status="failed", reason=str(e) or type(e).__name__)
                self._save(fid, r)
            raise
        finally:
            s.closed_loop_listener = None
        with self._flock(fid):
            if out["flown"]:
                cl = out["closed_loop"]
                r.update(status="done", planned=cl["planned"], flown=cl["flown"], audit=cl["audit"],
                         stopped=cl["stopped"])
            else:
                r.update(status="not flown", reason=out["reason"])
            self._save(fid, r)
        return {"flown": out["flown"], "reason": out["reason"]}

    # ---------------------------------------------------------------- aircraft messages
    def ingest(self, token: str, batch: dict) -> dict:
        dev = self.store.device_by_token_hash(token_hash(token or ""))
        if dev is None:
            raise PermissionError("unknown device token")
        fid = dev["farm_id"]
        grid = farm_grid(self.farms.load(fid))
        session = self.sessions.get(fid)                    # loaded (maybe restored) outside the lock
        with self._flock(fid):
            r = self._get(fid)
            if r["source"] != "aircraft" or batch.get("run_id") != r["run_id"]:
                raise NotFound("no live run with this id on the device's farm")
            if r["status"] != "running":
                raise ValueError(f"the live run is {r['status']}")
            if r.get("finishing"):
                raise Busy("the live run is being finished")
            ok, errs = validate_batch(batch, grid.n_cells)
            if ok["seq"] in r["seqs"]:
                return {"accepted": 0, "duplicate": True, "errors": errs}
            ok, off = self._on_field(ok, grid)
            errs += off
            nxt = copy.deepcopy(r)                          # applied in full, or not at all
            self._apply(fid, nxt, ok, grid, session)
            nxt["seqs"].append(ok["seq"])                    # counted only once applied
            nxt["batches"] = len(nxt["seqs"])
            self._save(fid, nxt)
            r.clear()
            r.update(nxt)
        self.store.touch_device(dev["id"])
        return {"accepted": len(ok["telemetry"]) + len(ok["cells"]), "duplicate": False, "errors": errs}

    @staticmethod
    def _on_field(b: dict, grid) -> tuple[dict, list[str]]:
        """Telemetry within MAX_OFF_FIELD_M of the field, in grid-local metres; the rest rejected."""
        if not b["telemetry"]:
            return b, []
        from pyproj import Transformer
        from shapely import distance, points
        tr = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
        xs, ys = tr.transform([p["lon"] for p in b["telemetry"]], [p["lat"] for p in b["telemetry"]])
        dist = distance(grid.polygon_local, points(np.column_stack([xs, ys])))
        keep, errs = [], []
        for i, (p, x, y, dm) in enumerate(zip(b["telemetry"], xs, ys, dist)):
            if not np.isfinite(dm) or dm > MAX_OFF_FIELD_M:
                errs.append(f"telemetry {i}: outside the field ({dm / 1000:.1f} km from its edge)"
                            if np.isfinite(dm) else f"telemetry {i}: outside the field")
            else:
                keep.append({**p, "x": float(x), "y": float(y)})
        return {**b, "telemetry": keep}, errs

    def _apply(self, fid: str, r: dict, b: dict, grid, session=None) -> None:
        n = grid.n_cells
        if b["telemetry"]:
            pts = sorted(({"x": p["x"], "y": p["y"], "z": p["alt_agl_m"], "spraying": p["spraying"],
                           "sortie": p["sortie"], "t": p["t"]} for p in b["telemetry"]), key=lambda p: str(p["t"]))
            r["drone"] = {k: pts[-1][k] for k in ("x", "y", "z", "sortie")}
            last = r.setdefault("last_tel", {})
            done = r.setdefault("sortie_cells", {})
            add = np.zeros(n, bool)
            for k in sorted({p["sortie"] for p in pts}):
                own = [p for p in pts if p["sortie"] == k]
                prev = last.get(str(k))
                track = ([prev] if prev is not None else []) + own   # the segment across the batch gap
                sprayed = cells_along(track, grid, r["swath"], sortie=k)
                before = np.zeros(n, bool)
                before[done.get(str(k), [])] = True
                add |= sprayed & ~before                        # a sortie doses a cell once
                done[str(k)] = np.where(before | sprayed)[0].tolist()
                last[str(k)] = own[-1]
            r["dose"] = r["dose"] + np.where(add, r["rate"], 0.0)
            fl = r["flown"]
            fl["sorties"] = max(fl["sorties"], max(p["sortie"] for p in pts))
            fl["litres"] = round(float(r["dose"].sum() * grid.cell_m ** 2 / 1e4), 2)
        seen = np.zeros(n, bool)
        if b["cells"]:
            idx = np.array([c["cell"] for c in b["cells"]])
            est_c, sd_c = estimate_from_counts([c["weeds"] for c in b["cells"]], [c["area_m2"] for c in b["cells"]],
                                               [c["gsd_m"] for c in b["cells"]], r["das"])
            est, sd = np.full(n, np.nan), np.full(n, np.inf)
            est[idx], sd[idx] = est_c, sd_c                      # a cell twice in one batch: last wins
            seen[idx] = True
            r["gsd_m"] = float(np.median([c["gsd_m"] for c in b["cells"]]))
            r["weeds"], r["weeds_sd"], r["seen"] = merge_observations(r["weeds"], r["weeds_sd"], r["seen"],
                                                                      est, sd, seen)
            r["detections"] = [{"cell": int(i), "est": round(float(r["weeds"][i]), 2)} for i in np.where(r["seen"])[0]]
            pred = self._pred(r)
            dw = deviation_weeds(pred, est, sd, seen)
            dd = deviation_dose(pred, r["dose"], r["dose"] > 0)
            r["deviation"] = {d.channel: d.cells.tolist() for d in (dw, dd) if d.significant}
        else:
            dw = dd = None
        if r["mode"] == "closed loop":
            s = session if session is not None else self.sessions.get(fid)
            tg, why = remaining_targets(s.twin, r["weeds"], r["dose"], r["rate"], r["product"],
                                        original=r["orig_targets"])
            for d in (dw, dd):
                if d is not None and d.significant:
                    r["audit"].append({"t": now(), "batch": b["seq"], "channel": d.channel,
                                       "correction": d.correction, "reason": f"{d.summary}; {why}"})
        else:
            tg = r["orig_targets"] & (r["dose"] < 0.9 * r["rate"])
        r["targets"] = tg

    @staticmethod
    def _pred(r: dict):
        from fa_runner.closedloop.prediction import CC_SD, Prediction
        return Prediction(r["op_id"], r["product"], r["pred_mean"], r["pred_sd"], float("nan"), CC_SD,
                          r["orig_targets"], r["pred_dose"], r["planned"]["sorties"], r["planned"]["time_min"],
                          r["planned"]["litres"], r["day"], r["rate"])

    # ---------------------------------------------------------------- operator
    def freeze(self, fid: str) -> dict:
        with self._flock(fid):
            r = self._get(fid)
            if r["mode"] != "open loop":
                r["mode"] = "open loop"
                r["audit"].append({"t": now(), "channel": "plan", "correction": "plan frozen by the operator",
                                   "reason": "corrections stop; the approved targets stand"})
                if r["source"] == "aircraft":
                    r["targets"] = r["orig_targets"] & (r["dose"] < 0.9 * r["rate"])
                self._save(fid, r)
        self.store.add_event(fid, "live_run_frozen", {"run": r["run_id"]})
        return self.state(fid)

    def next_sortie_kml(self, fid: str) -> bytes:
        r = self._get(fid)
        if r["source"] != "aircraft":
            raise ValueError("the next sortie file is for runs flown by the aircraft")
        farm = self.farms.get(fid)
        return self.exports.targets_kml(fid, f"{farm['name']} - next sortie ({r['op_id']})", r["targets"])

    def finish(self, fid: str) -> dict:
        """Book the job as flown (the run's as-applied dose) and hand what the camera saw to the
        twin, as one logged session action. The run is finished only once that succeeded."""
        with self._flock(fid):
            r = self._get(fid)
            if r["source"] != "aircraft" or r["status"] != "running" or r.get("finishing"):
                raise ValueError("only a running aircraft live run can be finished")
            r["finishing"] = True
        path = None
        try:
            grid = farm_grid(self.farms.load(fid))
            cells = np.where(r["seen"])[0]
            ref, obs = None, None
            if len(cells):
                ref = new_id("scan_")
                sd = [None if not np.isfinite(v) else float(v) for v in r["weeds_sd"][cells]]
                saved = {"live_run": r["run_id"], "cells": cells.tolist(), "est": r["weeds"][cells].tolist(),
                         "sd": sd, "gsd_m": r["gsd_m"] or 0.003, "day_index": r["das"]}
                d = self.store.farm_dir(fid) / "scans"
                d.mkdir(exist_ok=True)
                path = d / f"{ref}.json"
                path.write_text(json.dumps(saved))
                obs = obs_from_saved(saved, grid)
            dose = np.asarray(r["dose"], float)
            if dose.sum() > 0 or obs is not None:
                fl = r["flown"]
                record = {"day": r["day"], "dose_l_ha": [round(float(v), 4) for v in dose],
                          "litres": fl.get("litres") or None, "sorties": fl.get("sorties") or 1,
                          "time_min": fl.get("time_min") or None, "run_id": r["run_id"]}
                self.sessions.act(fid, "book_live_run", {"op_id": r["op_id"], "record": record, "ref": ref,
                                                         "obs": obs})
        except Exception:
            if path is not None and path.exists():
                path.unlink()
            with self._flock(fid):
                r.pop("finishing", None)
            raise
        with self._flock(fid):
            r.pop("finishing", None)
            r["status"] = "finished"
            self._save(fid, r)
        self.store.add_event(fid, "live_run_finished", {"run": r["run_id"], "cells_seen": int(len(cells))})
        return self.state(fid)

    # ---------------------------------------------------------------- view
    def state(self, fid: str) -> dict:
        r = self._get(fid)
        out = {k: r.get(k) for k in ("run_id", "source", "op_id", "product", "mode", "status", "valve", "started",
                                     "sorties", "audit", "planned", "flown", "batches", "drone", "detections",
                                     "deviation", "reason", "stopped", "track")}
        if r["source"] == "aircraft":
            out["remaining"] = np.where(r["targets"])[0].tolist()
            out["cells_seen"] = int(np.asarray(r["seen"]).sum())
            out["footprint_m"] = SprayerCamera().footprint_m(r["height"])
        else:
            out["remaining"] = r.get("remaining", [])
            out["cells_seen"] = len(r.get("seen_cells", []))
            out["footprint_m"] = SprayerCamera().footprint_m(self.sessions.get(fid).flight.height_m)
        out["deviation"] = out["deviation"] or {}
        out["sorties"] = out["sorties"] or []
        out["audit"] = out["audit"] or []
        return out
