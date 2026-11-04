"""Closed loop with the stock K3-A Pro: no hardware added to the aircraft.

The K3-A Pro takes no commands in flight and its data link is closed, so the loop closes
between sorties, through JIYI's own Agri Assistant app:

    plan the job -> split it into tank-sized blocks -> block 1 as KML (to the app, by hand
    or pushed over ADB) -> the K3-A Pro flies it (JIYI resumes a broken-off block itself)
    -> its flight record (litres, area, time) comes back (upload, or pulled over ADB)
    -> the twin re-plans what is left (its own spray rule on the latest map, the weather
    window for the next sortie) -> the next block -> ... -> finish: the job is booked in
    the twin with the as-applied dose of every block (one logged, replayable action).

Each block's record is checked against the block's plan: under 90 % of the planned area or
litres = the block is partly done and stays in the plan (fly it again: Agri Assistant resumes
from its breakpoint); a dose far over the rate is flagged.
"""
from __future__ import annotations

import json
import threading

import numpy as np

from fa_runner.closedloop.prediction import freeze_prediction
from fa_runner.closedloop.replan import remaining_targets
from fa_runner.scenarios import flight_record

from .errors import Busy, NotFound
from .scene import farm_grid
from .store import new_id, now

PARTIAL = 0.90          # a block is done when the record covers >= 90 % of its planned area
TANK_USE = 0.95         # plan blocks to 95 % of the tank


def serpentine(mask_cells: np.ndarray, grid) -> np.ndarray:
    """Target cell indices in boustrophedon order over the grid rows (compact blocks)."""
    rc = np.argwhere(grid.mask)                      # cell index -> (row, col), row-major
    idx = np.where(mask_cells)[0]
    rows, cols = rc[idx, 0], rc[idx, 1]
    key = rows * 100000 + np.where(rows % 2 == 0, cols, 99999 - cols)
    return idx[np.argsort(key, kind="stable")]


def blocks_for(targets: np.ndarray, grid, rate_l_ha: float, tank_l: float) -> list[list[int]]:
    cell_ha = grid.cell_m ** 2 / 1e4
    per_block = max(1, int((tank_l * TANK_USE) / (rate_l_ha * cell_ha)))
    order = serpentine(np.asarray(targets, bool), grid).tolist()
    return [order[i:i + per_block] for i in range(0, len(order), per_block)]


class StockLoopService:
    def __init__(self, store, farms, sessions, exports, rc=None):
        self.store, self.farms, self.sessions, self.exports = store, farms, sessions, exports
        self.rc = rc                                  # an RcLink (ADB) or None
        self._runs: dict[str, dict] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, fid):
        with self._guard:
            return self._locks.setdefault(fid, threading.Lock())

    def _path(self, fid):
        d = self.store.farm_dir(fid) / "stock_loop"
        d.mkdir(exist_ok=True)
        return d / "current.json"

    def _save(self, fid, r):
        self._path(fid).write_text(json.dumps(r))

    def _get(self, fid) -> dict:
        r = self._runs.get(fid)
        if r is None:
            p = self._path(fid)
            if not p.exists():
                raise NotFound("no stock K3-A Pro job on this farm")
            r = json.loads(p.read_text())
            self._runs[fid] = r
        return r

    # ---------------------------------------------------------------- plan
    def start(self, fid: str, op_id: str, tank_l: float | None = None) -> dict:
        """``tank_l``: litres the operator fills per sortie (default: the aircraft's tank)."""
        try:
            cur = self._get(fid)
        except NotFound:
            cur = None
        if cur is not None and cur["status"] == "running":
            raise Busy(f"stock job {cur['run_id']} is running on this farm: finish it first")
        s = self.sessions.get(fid)
        op = s.ops.get(op_id)
        if op is None:
            raise NotFound(f"no operation {op_id}")
        if op.status not in ("proposed", "approved") or op.product not in ("herbicide", "fungicide"):
            raise ValueError(f"operation {op_id} is a {op.product} job, {op.status}: nothing to fly")
        grid = farm_grid(self.farms.load(fid))
        pred = freeze_prediction(s, op)
        flight = s.flight if not op.settings else type(s.flight)(**op.settings)
        tank = float(tank_l or getattr(s.spec, "tank_l", 10.0))
        if not 0.5 <= tank <= 100:
            raise ValueError("tank fill must be 0.5-100 L")
        blocks = blocks_for(pred.targets, grid, flight.rate_l_ha, tank)
        if not blocks:
            raise ValueError("the job has no target cells")
        r = {"run_id": new_id("stk_"), "op_id": op_id, "product": op.product, "status": "running",
             "started": now(), "rate": flight.rate_l_ha, "tank_l": tank, "cell_ha": grid.cell_m ** 2 / 1e4,
             "targets": np.where(pred.targets)[0].tolist(), "blocks": [{"n": i + 1, "cells": b, "status": "planned",
                                                                        "records": []} for i, b in enumerate(blocks)],
             "dose": [0.0] * grid.n_cells, "audit": [], "current": 1, "pushed": None,
             "planned": {"litres": round(pred.litres, 2), "sorties": len(blocks), "time_min": round(pred.time_min, 1)}}
        with self._lock(fid):
            self._runs[fid] = r
            self._save(fid, r)
        self.store.add_event(fid, "stock_loop_started", {"run": r["run_id"], "op": op_id, "blocks": len(blocks)})
        self._push_current(fid, r)
        return self.state(fid)

    # ---------------------------------------------------------------- per sortie
    def block_kml(self, fid: str, n: int | None = None) -> bytes:
        r = self._get(fid)
        b = self._block(r, n or r["current"])
        grid = farm_grid(self.farms.load(fid))
        mask = np.zeros(grid.n_cells, bool)
        mask[b["cells"]] = True
        farm = self.farms.get(fid)
        return self.exports.targets_kml(fid, f"{farm['name']} - block {b['n']} of {len(r['blocks'])} ({r['op_id']})",
                                        mask)

    def _block(self, r, n):
        if not 1 <= n <= len(r["blocks"]):
            raise NotFound(f"no block {n}")
        return r["blocks"][n - 1]

    def record(self, fid: str, rec: dict, n: int | None = None) -> dict:
        """The JIYI flight record of the block just flown: {"litres", "area_ha", "flight_min"?}."""
        try:
            litres, area = float(rec["litres"]), float(rec["area_ha"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError("a flight record needs litres and area_ha") from e
        if litres < 0 or area <= 0:
            raise ValueError("litres must be >= 0 and area_ha > 0")
        with self._lock(fid):
            r = self._get(fid)
            if r["status"] != "running":
                raise ValueError(f"the stock job is {r['status']}")
            b = self._block(r, n or r["current"])
            if b["status"] == "done":
                raise ValueError(f"block {b['n']} is already done")
            planned_ha = len(b["cells"]) * r["cell_ha"]
            b["records"].append({"t": now(), "litres": litres, "area_ha": area,
                                 "flight_min": rec.get("flight_min"), "source": rec.get("source", "operator")})
            got_ha = sum(x["area_ha"] for x in b["records"])
            got_l = sum(x["litres"] for x in b["records"])
            dose = got_l / max(got_ha, 1e-9)
            share = min(1.0, got_ha / planned_ha)
            d = np.asarray(r["dose"], float)
            d[b["cells"]] = dose * share           # no per-cell trace: the block's mean as-applied dose
            r["dose"] = d.round(4).tolist()
            if share >= PARTIAL:
                b["status"] = "done"
                why = f"block {b['n']}: {got_ha:.2f} of {planned_ha:.2f} ha, {got_l:.1f} L ({dose:.1f} L/ha)"
            else:
                b["status"] = "partial"
                why = (f"block {b['n']} partly flown ({got_ha:.2f} of {planned_ha:.2f} ha): fly it again, "
                       "Agri Assistant resumes from its breakpoint")
            if dose > 1.5 * r["rate"]:
                why += f"; dose {dose:.1f} L/ha is over 150 % of the rate {r['rate']:.1f}: check the app's rate"
            r["audit"].append({"t": now(), "block": b["n"], "channel": "record", "correction": b["status"],
                               "reason": why})
            self._replan(fid, r)
            self._save(fid, r)
        self._push_current(fid, r)
        return self.state(fid)

    def _replan(self, fid: str, r: dict) -> None:
        """What is left after this sortie, by the twin's own rule on its latest map."""
        s = self.sessions.get(fid)
        dose = np.asarray(r["dose"], float)
        orig = np.zeros(len(dose), bool)
        orig[r["targets"]] = True
        if s.twin is not None and r["product"] == "herbicide":
            want, why = remaining_targets(s.twin, s.twin.digital.weed_map, dose, r["rate"], r["product"], orig)
        else:
            want, why = orig & (dose < PARTIAL * r["rate"]), "approved targets not yet at 90 % of the rate"
        for b in r["blocks"]:
            if b["status"] in ("done", "partial"):
                continue
            keep = [c for c in b["cells"] if want[c]]
            if len(keep) != len(b["cells"]):
                r["audit"].append({"t": now(), "block": b["n"], "channel": "plan",
                                   "correction": f"block {b['n']}: {len(b['cells']) - len(keep)} cells dropped",
                                   "reason": why})
                b["cells"] = keep
                if not keep:
                    b["status"] = "dropped"
        nxt = next((b for b in r["blocks"] if b["status"] == "partial"), None) or \
            next((b for b in r["blocks"] if b["status"] == "planned"), None)
        r["current"] = nxt["n"] if nxt else None
        rec = s.world.weather(s.day)
        if nxt and rec is not None and not s.window.ok(flight_record(rec)):
            r["audit"].append({"t": now(), "block": nxt["n"], "channel": "weather",
                               "correction": "wait before the next sortie",
                               "reason": "outside the spray window: " + ", ".join(s.window.reasons(flight_record(rec)))})

    def _push_current(self, fid: str, r: dict) -> None:
        if self.rc is None or r.get("current") is None:
            return
        try:
            name = f"{r['run_id']}_block{r['current']}.kml"
            self.rc.push_bytes(self.block_kml(fid), name)
            r["pushed"] = {"block": r["current"], "file": name, "t": now(), "error": None}
        except Exception as e:  # noqa: BLE001 -- the KML is still downloadable from the console
            r["pushed"] = {"block": r["current"], "file": None, "t": now(), "error": str(e)}
        with self._lock(fid):
            self._save(fid, r)

    def pull_records(self, fid: str) -> dict:
        """New flight-record files on the remote (ADB) -> records of the current block."""
        if self.rc is None:
            raise ValueError("no remote controller linked (connect it by USB or Wi-Fi ADB)")
        from fa_drone.jiyi_io import import_job_records
        r = self._get(fid)
        seen = set(r.get("pulled", []))
        added = 0
        for name, text in self.rc.new_records(seen):
            recs, _ = import_job_records(text)
            for jr in recs:
                if jr.total_l is None:
                    continue
                self.record(fid, {"litres": jr.total_l, "area_ha": jr.area_ha, "flight_min": jr.flight_min,
                                  "source": f"remote: {name}"})
                added += 1
            seen.add(name)
        r = self._get(fid)
        r["pulled"] = sorted(seen)
        with self._lock(fid):
            self._save(fid, r)
        return {"added": added, "state": self.state(fid)}

    # ---------------------------------------------------------------- finish
    def finish(self, fid: str) -> dict:
        with self._lock(fid):
            r = self._get(fid)
            if r["status"] != "running":
                raise ValueError(f"the stock job is {r['status']}")
            if r.get("finishing"):
                raise Busy("the stock job is being finished")
            r["finishing"] = True
        try:
            dose = np.asarray(r["dose"], float)
            if dose.sum() > 0:
                s = self.sessions.get(fid)
                litres = sum(x["litres"] for b in r["blocks"] for x in b["records"])
                minutes = sum(float(x.get("flight_min") or 0) for b in r["blocks"] for x in b["records"])
                flown = sum(1 for b in r["blocks"] if b["records"])
                record = {"day": s.day.isoformat(), "dose_l_ha": dose.round(4).tolist(), "litres": round(litres, 2),
                          "sorties": flown, "time_min": minutes or None, "run_id": r["run_id"]}
                self.sessions.act(fid, "book_live_run", {"op_id": r["op_id"], "record": record, "ref": None,
                                                         "obs": None})
        except Exception:
            with self._lock(fid):
                r.pop("finishing", None)
            raise
        with self._lock(fid):
            r.pop("finishing", None)
            r["status"] = "finished"
            self._save(fid, r)
        self.store.add_event(fid, "stock_loop_finished", {"run": r["run_id"]})
        return self.state(fid)

    # ---------------------------------------------------------------- view
    def state(self, fid: str) -> dict:
        r = self._get(fid)
        blocks = [{"n": b["n"], "cells": len(b["cells"]), "status": b["status"],
                   "area_ha": round(len(b["cells"]) * r["cell_ha"], 3),
                   "litres_planned": round(len(b["cells"]) * r["cell_ha"] * r["rate"], 2),
                   "records": b["records"]} for b in r["blocks"]]
        done = [b for b in blocks if b["status"] == "done"]
        return {"run_id": r["run_id"], "op_id": r["op_id"], "product": r["product"], "status": r["status"],
                "rate_l_ha": r["rate"], "tank_l": r["tank_l"], "current": r["current"], "blocks": blocks,
                "planned": r["planned"], "audit": r["audit"][-50:], "pushed": r.get("pushed"),
                "flown": {"litres": round(sum(x["litres"] for b in r["blocks"] for x in b["records"]), 2),
                          "blocks_done": len(done), "blocks": len(blocks)},
                "remote": None if self.rc is None else self.rc.status()}
