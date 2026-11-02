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
gets the prescribed rate once per batch (flow is not yet used).
"""
from __future__ import annotations

import json
import threading

import numpy as np

from fa_core.live_msgs import validate_batch
from fa_runner.closedloop.prediction import deviation_dose, deviation_weeds, freeze_prediction
from fa_runner.closedloop.replan import merge_observations, remaining_targets
from fa_scan.measure import ScanObservation
from fa_scan.sprayer_camera import SprayerCamera, cells_along, estimate_from_counts

from .devices import token_hash
from .errors import NotFound
from .scene import farm_grid
from .store import new_id, now

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
        self._lock = threading.Lock()
        jobs.register("live_run", self._job)

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
            with self._lock:
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
             "deviation": {}}
        with self._lock:
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
            with self._lock:
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
        finally:
            s.closed_loop_listener = None
        with self._lock:
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
        with self._lock:
            r = self._get(fid)
            if r["source"] != "aircraft" or batch.get("run_id") != r["run_id"]:
                raise NotFound("no live run with this id on the device's farm")
            if r["status"] != "running":
                raise ValueError(f"the live run is {r['status']}")
            grid = farm_grid(self.farms.load(fid))
            ok, errs = validate_batch(batch, grid.n_cells)
            if ok["seq"] in r["seqs"]:
                return {"accepted": 0, "duplicate": True, "errors": errs}
            r["seqs"].append(ok["seq"])
            r["batches"] = len(r["seqs"])
            self._apply(fid, r, ok, grid)
            self._save(fid, r)
        self.store.touch_device(dev["id"])
        return {"accepted": len(ok["telemetry"]) + len(ok["cells"]), "duplicate": False, "errors": errs}

    def _apply(self, fid: str, r: dict, b: dict, grid) -> None:
        n = grid.n_cells
        if b["telemetry"]:
            from pyproj import Transformer
            tr = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
            pts = []
            for p in b["telemetry"]:
                x, y = tr.transform(p["lon"], p["lat"])
                pts.append({"x": x, "y": y, "z": p["alt_agl_m"], "spraying": p["spraying"], "sortie": p["sortie"],
                            "t": p["t"]})
            r["drone"] = {k: pts[-1][k] for k in ("x", "y", "z", "sortie")}
            sprayed = np.zeros(n, bool)
            for k in sorted({p["sortie"] for p in pts}):
                sprayed |= cells_along(pts, grid, r["swath"], sortie=k)
            r["dose"] = r["dose"] + np.where(sprayed, r["rate"], 0.0)
            fl = r["flown"]
            fl["sorties"] = max(fl["sorties"], max(p["sortie"] for p in pts))
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
            s = self.sessions.get(fid)
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
        with self._lock:
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
        with self._lock:
            r = self._get(fid)
            if r["source"] != "aircraft" or r["status"] != "running":
                raise ValueError("only a running aircraft live run can be finished")
            grid = farm_grid(self.farms.load(fid))
            cells = np.where(r["seen"])[0]
            r["status"] = "finished"
            self._save(fid, r)
        if len(cells):
            ref = new_id("scan_")
            sd = [None if not np.isfinite(v) else float(v) for v in r["weeds_sd"][cells]]
            saved = {"live_run": r["run_id"], "cells": cells.tolist(), "est": r["weeds"][cells].tolist(), "sd": sd,
                     "gsd_m": r["gsd_m"] or 0.003, "day_index": r["das"]}
            d = self.store.farm_dir(fid) / "scans"
            d.mkdir(exist_ok=True)
            (d / f"{ref}.json").write_text(json.dumps(saved))
            self.sessions.act(fid, "scan_now", {"obs": obs_from_saved(saved, grid), "ref": ref})
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
