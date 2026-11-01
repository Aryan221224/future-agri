"""The closed-loop season as a stepper.

``SeasonSession`` runs the season one morning at a time against a ``World``:

* **morning** -- the world advances through yesterday, the twin receives
  yesterday's observed weather; on the scan day the camera drone scans and the
  twin ingests the DigitalFarm (herbicide decision from the weed map); weekly
  from 4 weeks after sowing the twin plans fungicide with the real ensemble
  forecast issued that day, scouting when undecided.
* **flights** -- every approved spray whose due date has come is flown by the
  drone twin if the day's weather passes the spray window; what it actually
  deposited lands on the world.

With ``approvals="auto"`` every proposal is approved as it is made -- exactly
the batch season (``run_season`` wraps this). With ``approvals="manual"`` the
session pauses after a morning that produced proposals, so an operator can
approve, defer or reject before the day's flights. Every public call is logged
in ``actions``; ``restore`` replays them, so a session survives a restart.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

import numpy as np

from fa_drone.mission import MissionSettings, simulate_mission
from fa_drone.power import PowerModel
from fa_drone.spec import DroneSpec, load_spec
from fa_drone.spray import SprayModel
from fa_farm.regions import load_region
from fa_farm.truth import AsApplied
from fa_scan.digital import build_digital_farm

from .economics import Economics
from .optimise import REF_HA_PER_H
from .scenarios import ForecastFeed, flight_record, mission_weather, spray_window
from .twinfarm import TwinFarm
from .world import VirtualWorld, WaitingForData

POLICIES = ("none", "calendar", "twin", "twin_alert")
#: Days a due job may wait for flyable weather. Herbicide stays useful for two weeks
#: (weeds grow but are still controlled); a fungicide not flown within a week is
#: re-decided by the next weekly plan instead.
MAX_WAIT_DAYS = {"herbicide": 14, "fungicide": 6}
DAY = dt.timedelta(days=1)
OPEN = ("proposed", "approved")


@dataclass
class SeasonResult:
    policy: str
    seed: int
    yield_dry_kg_ha: float
    yield_marketed_kg_ha: float
    revenue_ha: float
    costs_ha: dict
    net_ha: float
    currency: str
    fungicide_sprays: int
    herbicide: str
    herbicide_treated_frac: float
    missions: int
    litres: float
    sorties: int
    battery_swaps: int
    drone_time_min: float
    off_target_l: float
    mean_cv_pct: float
    mean_skipped_frac: float
    scans: int
    blight_infection_day: str | None
    final_blight_severity: float
    weed_loss_pct: float
    forecast_sources: dict
    flight: dict
    decisions: list = field(default_factory=list)
    missions_log: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    as_applied_last: list | None = None

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("decisions")
        d.pop("missions_log")
        d.pop("as_applied_last")
        return d


@dataclass
class Operation:
    id: str
    product: str                    # herbicide | fungicide | scan
    due: dt.date
    status: str                     # proposed | approved | flown | dropped | rejected
    targets: np.ndarray | None      # treated cells (None = whole field)
    created: dt.date
    reason: str = ""
    settings: dict | None = None    # flight override for this job
    result: dict | None = None      # KPIs once flown

    def as_dict(self) -> dict:
        t = self.targets
        return {"id": self.id, "product": self.product, "due": self.due.isoformat(),
                "status": self.status, "created": self.created.isoformat(), "reason": self.reason,
                "targets_frac": 1.0 if t is None else round(float(np.mean(t)), 4),
                "settings": self.settings, "result": self.result}


def _date(v):
    return dt.date.fromisoformat(v) if isinstance(v, str) else v


class SeasonSession:
    def __init__(self, vf, policy: str = "twin", seed: int = 0, feed: ForecastFeed | None = None,
                 spec: DroneSpec | None = None, flight: MissionSettings | None = None,
                 scan_das: int = 45, scan_gsd_m: float = 0.005, camera: str = "p4rtk",
                 scout_gsd_m: float = 0.01, plan_members: int = 10, objective: str = "mean",
                 cache=None, expected_yield_kg_ha: float | None = None, approvals: str = "auto",
                 world=None, trace: bool = True, snapshot_every: int = 7, keep_tracks: bool = False):
        if policy not in POLICIES:
            raise ValueError(f"policy must be one of {POLICIES}")
        if approvals not in ("auto", "manual"):
            raise ValueError("approvals must be 'auto' or 'manual'")
        self.vf, self.policy, self.seed, self.approvals = vf, policy, seed, approvals
        self.spec = spec or load_spec()
        self.flight = flight or MissionSettings()
        self.scan_das, self.scan_gsd_m, self.camera = scan_das, scan_gsd_m, camera
        self.scout_gsd_m, self.plan_members = scout_gsd_m, plan_members
        self.trace_on, self.snapshot_every, self.keep_tracks = trace, snapshot_every, keep_tracks
        self.archetype = a = vf.spec.archetype
        self.region = load_region(a.region)
        self.econ = Economics.from_region(self.region, a.crop)
        self.window = spray_window(self.region, self.spec)
        self.rng = np.random.default_rng(10_000 + seed)
        self.world = world or VirtualWorld(vf, seed)
        self.grid = self.world.grid
        lat, lon = vf.spec.centroid
        self.feed = feed or ForecastFeed(lat, lon, vf.history, cache)
        self.pm, self.sm = PowerModel.calibrate(self.spec), SprayModel(self.spec)
        self.notes = list(self.region.warnings())

        self.twin = None
        if policy.startswith("twin"):
            inoc = self.world.inoculum_alert() if policy == "twin_alert" else None
            if expected_yield_kg_ha is None:
                from .perfect import prior_yield
                expected_yield_kg_ha = prior_yield(vf) or None
            self.twin = TwinFarm(a, self.region, lat, lon, self.world.pre_sowing(), vf.spec.soil,
                                 self.world.sowing, self.world.harvest, inoculum=inoc,
                                 objective=objective, plan_members=plan_members,
                                 expected_yield_kg_ha=expected_yield_kg_ha)

        self.costs = {"fungicide_product": 0.0, "herbicide_product": 0.0, "application": 0.0,
                      "scans": 0.0}
        self.st = {"litres": 0.0, "sorties": 0, "swaps": 0, "time": 0.0, "off": 0.0, "fung": 0,
                   "scans": 0, "herb": "none", "herb_frac": 0.0}
        self.ops: dict[str, Operation] = {}
        self.missions_log: list[dict] = []
        self._cvs: list[float] = []
        self._skips: list[float] = []
        self.last_aa = None
        self.last_digital = None
        self.blight_crop = self.world.blight_modelled
        self.windy_days = 0
        self.day = self.world.sowing + DAY
        self.phase = "morning"
        self.done = False
        self.events: list[dict] = []
        self.trace: list[dict] = []
        self.snapshots: dict[str, dict[str, np.ndarray]] = {}
        self.actions: list[dict] = []
        self.flight_tracks: dict[str, list] = {}
        n = self.grid.n_cells
        self.dose_cum = {"fungicide": np.zeros(n), "herbicide": np.zeros(n)}
        self._new_today: list[str] = []
        self._event_today = False
        self._seq = 0
        self._leftover: list[Operation] = []

    # ------------------------------------------------------------------ bookkeeping
    def _log(self, action: str, params: dict) -> dict:
        entry = {"action": action, "params": params}
        self.actions.append(entry)
        return entry

    def _unlog(self, entry: dict) -> None:
        if self.actions and self.actions[-1] is entry:
            self.actions.pop()

    def _event(self, kind: str, text: str, **data) -> None:
        self.events.append({"day": self.day.isoformat(), "kind": kind, "text": text, **data})
        if kind in ("scan", "flight"):
            self._event_today = True

    def _open(self) -> list[Operation]:
        return [o for o in self.ops.values() if o.status in OPEN and o.product != "scan"]

    def _propose(self, product: str, due: dt.date, targets, reason: str) -> Operation:
        self._seq += 1
        oid = f"{product}-{due.isoformat()}-{self._seq}"
        status = "approved" if (self.approvals == "auto" and product != "scan") else "proposed"
        op = Operation(oid, product, due, status, targets, self.day, reason)
        self.ops[oid] = op
        self._new_today.append(oid)
        self._event("proposal", reason, op=oid, product=product, due=due.isoformat(), status=status)
        return op

    def _op(self, op_id: str) -> Operation:
        if op_id not in self.ops:
            raise ValueError(f"unknown operation {op_id}")
        return self.ops[op_id]

    # ------------------------------------------------------------------ the day
    def _morning(self) -> None:
        d, w, a = self.day, self.world, self.archetype
        y = d - DAY
        rec = w.weather(y)
        if rec is None:
            raise WaitingForData(f"observed weather for {y} has not arrived yet")
        w.advance_to(y)
        if self.twin is not None:
            self.twin.add_weather([rec])
        das = (d - w.sowing).days
        self._new_today = []

        # ---- herbicide
        if a.weeds:
            if self.policy == "calendar" and das == 30:
                self._propose("herbicide", d, None, "calendar: blanket herbicide 30 days after sowing")
                self.st["herb"], self.st["herb_frac"] = "blanket", 1.0
            if self.twin is not None and das == self.scan_das:
                self._scheduled_scan(d, herbicide=True)
        elif self.twin is not None and das == self.scan_das:
            self._scheduled_scan(d, herbicide=False)

        # ---- fungicide
        if self.blight_crop and das >= 28:
            if self.policy == "calendar" and (das - 30) % 10 == 0 and das >= 30:
                self._propose("fungicide", d, None, "calendar: fungicide every 10 days from day 30")
            if self.twin is not None and (das - 28) % 7 == 0:
                self._weekly_plan(d, das)

        if self.trace_on:
            self._trace_row(d, das, rec)

    def _scheduled_scan(self, d: dt.date, herbicide: bool) -> None:
        obs = self.world.scan(d, self.scan_gsd_m, self.camera, self.rng, self.archetype.crop)
        if obs is None:                     # live field: waits for the operator's upload
            if not any(o.product == "scan" and o.status in OPEN for o in self.ops.values()):
                self._propose("scan", d, None, f"camera-drone scan due (GSD {self.scan_gsd_m * 1000:.0f} mm)")
            return
        self._ingest(d, obs, herbicide=herbicide, purpose="pass")

    def _ingest(self, d: dt.date, obs, herbicide: bool, purpose: str) -> None:
        self.costs["scans"] += self.econ.scan_ha
        self.st["scans"] += 1
        dfarm = build_digital_farm(obs, self.grid)
        self.last_digital = dfarm
        if self.twin is not None:
            self.twin.ingest(dfarm, d - DAY, seed=self.seed)
        self._event("scan", f"{purpose} scan ingested (GSD {obs.gsd_m * 1000:.0f} mm)", purpose=purpose,
                    gsd_m=obs.gsd_m, weeds_m2=round(float(np.mean(obs.weed_density_est)), 2),
                    blight_seen=bool(obs.blight_flag.any()))
        if herbicide and self.twin is not None:
            self.st["herb"] = self.twin.herbicide_option
            tg = self.twin.weed_targets()
            self.st["herb_frac"] = float(tg.mean())
            if tg.any():
                self._propose("herbicide", d, None if tg.all() else tg,
                              f"twin: {self.twin.herbicide_option} herbicide from the scan's weed map "
                              f"({100 * float(tg.mean()):.0f}% of the field)")

    def _weekly_plan(self, d: dt.date, das: int) -> None:
        twin = self.twin
        mem = self.feed.members(d, twin.field.records, self.world.harvest, self.plan_members,
                                seed=self.seed * 31 + das)
        plan = twin.weekly_plan(d, mem, seed=self.seed * 7919 + das)
        if plan.get("scout"):
            obs = self.world.scan(d, self.scout_gsd_m, self.camera, self.rng, self.archetype.crop)
            if obs is not None:
                self.costs["scans"] += self.econ.scan_ha
                self.st["scans"] += 1
                seen = bool(obs.blight_flag.any())
                twin.record_scouting(d, obs.blight_sev_est if seen else None)
                self._event("scan", "scouting flight for blight", purpose="scout", gsd_m=obs.gsd_m,
                            weeds_m2=round(float(np.mean(obs.weed_density_est)), 2), blight_seen=seen)
                plan = twin.weekly_plan(d, mem, seed=self.seed * 7919 + das + 1)
            else:
                self._event("scout_needed", "the twin asks for a scouting check for blight")
        fd = plan.get("fungicide")
        if fd is not None:
            self._event("plan", fd.reason, spray_day=str(fd.spray_day) if fd.spray_day else None,
                        p_spray_better=round(float(fd.p_spray_better), 3),
                        expected_gain=round(float(fd.expected_gain), 2))
        if fd is not None and fd.spray_day is not None and fd.spray_day <= d + dt.timedelta(days=6):
            if not any(o.product == "fungicide" for o in self._open()):
                self._propose("fungicide", max(d, fd.spray_day), None, f"twin: {fd.reason}")

    def _flights(self) -> None:
        d = self.day
        auto_fly = getattr(self.world, "has_truth", True)
        for op in list(self.ops.values()):
            if op.status not in OPEN or op.product == "scan" or op.due > d:
                continue
            if op.status == "approved" and auto_fly and self._fly(d, op):
                continue
            wait = MAX_WAIT_DAYS[op.product]
            if (d - op.due).days < wait:
                continue
            if op.status == "approved":
                why = (f"no feasible spray window within {wait} days" if auto_fly
                       else f"no flight recorded within {wait} days")
            else:
                why = f"not approved within {wait} days"
            op.status = "dropped"
            self.notes.append(f"{op.product} due {op.due} dropped: {why}")
            self._event("dropped", f"{op.product} due {op.due} dropped: {why}", op=op.id)

    def _fly(self, d: dt.date, op: Operation) -> bool:
        rec = self.world.weather(d)
        if rec is None:
            return False
        fr = flight_record(rec)
        if not self.window.ok(fr):
            self.windy_days += 1
            self._event("no_fly", f"{op.product}: outside the spray window ({', '.join(self.window.reasons(fr))})",
                        op=op.id)
            return False
        flight = MissionSettings(**op.settings) if op.settings else self.flight
        res = simulate_mission(self.grid, self.spec, flight, mission_weather(rec, self.rng),
                               targets=op.targets, seed=self.seed * 97 + len(self.missions_log),
                               rules=self.region.rules, power=self.pm, spray=self.sm,
                               record_track=self.keep_tracks)
        if not res.feasible:
            self.windy_days += 1
            self._event("no_fly", f"{op.product}: not flyable ({'; '.join(res.reasons)})", op=op.id)
            return False
        frac = res.dose_l_ha / flight.rate_l_ha
        self.world.apply(AsApplied(d, op.product, frac, res.kpis["off_target_frac"] * res.kpis["litres"],
                                   res.kpis["litres"]))
        self.last_aa = frac.round(3).tolist()
        econ = self.econ
        prod_ha = econ.fungicide_product_ha if op.product == "fungicide" else econ.herbicide_product_ha
        field_ha = self.grid.area_ha
        # product at label concentration for the litres actually sprayed
        self.costs[f"{op.product}_product"] += prod_ha * res.kpis["litres"] / (flight.rate_l_ha * field_ha)
        self.costs["application"] += (res.kpis["time_min"] / 60) * REF_HA_PER_H * econ.drone_service_ha / field_ha
        st = self.st
        st["litres"] += res.kpis["litres"]
        st["sorties"] += res.kpis["sorties"]
        st["swaps"] += res.kpis["battery_swaps"]
        st["time"] += res.kpis["time_min"]
        st["off"] += res.kpis["off_target_frac"] * res.kpis["litres"]
        self._cvs.append(res.kpis["cv_pct"])
        self._skips.append(res.kpis["skipped_frac"])
        self.missions_log.append({"day": d.isoformat(), "product": op.product, **res.kpis,
                                  "warnings": res.warnings})
        if op.product == "fungicide":
            st["fung"] += 1
            if self.twin is not None:
                self.twin.record_spray(d)
        self.dose_cum[op.product] += res.dose_l_ha
        op.status, op.result = "flown", dict(res.kpis)
        if self.keep_tracks:
            self.flight_tracks[op.id] = res.track
        self._event("flight", f"{op.product} flown: {res.kpis['litres']:.1f} L in {res.kpis['time_min']:.0f} min, "
                              f"{res.kpis['sorties']} sorties", op=op.id, kpis=dict(res.kpis))
        return True

    def _end_day(self) -> None:
        d = self.day
        if self.trace_on and (self._event_today or (d - self.world.sowing).days % self.snapshot_every == 0):
            self._snapshot(d)
        self._event_today = False
        self.day = d + DAY
        self.phase = "morning"
        if self.day > self.world.harvest:
            self.done = True
            self._leftover = self._open()
            for op in self._leftover:
                why = ("no feasible spray window" if op.status == "approved" else "never approved")
                self.notes.append(f"{op.product} due {op.due} not flown before harvest: {why}")

    def _step(self) -> tuple[list[dict], bool]:
        """One phase: a morning (pausing for approvals in manual mode), then its flights."""
        if self.done:
            return [], False
        n0 = len(self.events)
        if self.phase == "morning":
            self._morning()
            self.phase = "flights"
            if self.approvals == "manual" and self._new_today:
                return self.events[n0:], True
        self._flights()
        self._end_day()
        return self.events[n0:], False

    # ------------------------------------------------------------------ trace
    def _trace_row(self, d: dt.date, das: int, rec: dict) -> None:
        row = {"day": d.isoformat(), "das": das, "tmax": rec["TMAX"], "tmin": rec["TMIN"],
               "rain_mm": rec["RAIN"], "wind_ms": rec["WIND"]}
        s = self.world.truth_state()
        if s is not None:
            row.update(truth_lai=round(float(s.lai.mean()), 3), truth_cc=round(float(s.cc.mean()), 3),
                       truth_dvs=round(float(s.dvs.mean()), 3),
                       truth_weeds_m2=round(float(s.weeds_m2.mean()), 2),
                       truth_blight=round(float(s.blight_sev.mean()), 4))
        else:
            row.update(truth_lai=None, truth_cc=None, truth_dvs=None, truth_weeds_m2=None, truth_blight=None)
        tw: dict = {}
        if self.twin is not None:
            try:
                self.twin.field.advance_to(d)
                tw = self.twin.field.state()
            except Exception:  # noqa: BLE001 -- the trace must never stop the season
                tw = {}
        row.update(twin_lai=_r(tw.get("LAI"), 3), twin_dvs=_r(tw.get("DVS"), 3), twin_twso=_r(tw.get("TWSO"), 1))
        self.trace.append(row)

    def _snapshot(self, d: dt.date) -> None:
        s = self.world.truth_state()
        if s is not None:
            lai, cc, weeds, blight = s.lai, s.cc, s.weeds_m2, s.blight_sev
        elif self.last_digital is not None:
            df = self.last_digital
            cc = np.array([df.zones[z].cc for z in df.zone_of_cell])
            lai = -np.log(np.clip(1.0 - cc, 1e-3, 1.0)) / 0.6
            weeds = df.weed_map
            blight = df.blight_cells.astype(float) * df.blight_sev_est
        else:
            return
        f16 = lambda x: np.asarray(x, dtype=np.float16)  # noqa: E731
        self.snapshots[d.isoformat()] = {"lai": f16(lai), "cc": f16(cc), "weeds": f16(weeds),
                                         "blight": f16(blight),
                                         "dose_fungicide": f16(self.dose_cum["fungicide"]),
                                         "dose_herbicide": f16(self.dose_cum["herbicide"])}

    # ------------------------------------------------------------------ public API
    def step(self) -> list[dict]:
        """Advance one phase (a morning, or the flights of a paused day)."""
        entry = self._log("step", {})
        try:
            ev, _ = self._step()
        except WaitingForData:
            raise
        except Exception:
            self._unlog(entry)
            raise
        return ev

    def run_until(self, day=None, stop_on_decision: bool = False, max_days: int | None = None,
                  approve_all: bool = False) -> list[dict]:
        """Run mornings and flights until ``day`` has been processed (harvest when None), a
        decision is waiting (``stop_on_decision``), ``max_days`` days have passed, or the
        world has no new observation. ``approve_all`` approves every proposal as it comes."""
        self._log("run_until", {"day": day.isoformat() if isinstance(day, dt.date) else day,
                                "stop_on_decision": stop_on_decision, "max_days": max_days,
                                "approve_all": approve_all})
        day = _date(day)
        out: list[dict] = []
        days = 0
        while not self.done:
            if self.phase == "morning":
                if day is not None and self.day > day:
                    break
                if max_days is not None and days >= max_days:
                    break
            try:
                ev, paused = self._step()
            except WaitingForData as e:
                self._event("waiting", str(e))
                out.append(self.events[-1])
                break
            out += ev
            if self.phase == "morning":
                days += 1
            if paused:
                if approve_all:
                    for oid in list(self._new_today):
                        if self.ops[oid].status == "proposed" and self.ops[oid].product != "scan":
                            self._approve(oid)
                elif stop_on_decision:
                    break
        return out

    def _approve(self, op_id: str) -> None:
        op = self._op(op_id)
        if op.status != "proposed":
            raise ValueError(f"operation {op_id} is {op.status}, not proposed")
        op.status = "approved"
        self._event("approved", f"{op.product} due {op.due} approved", op=op_id)

    def approve(self, op_id: str) -> dict:
        self._op(op_id)
        if self.ops[op_id].product == "scan":
            raise ValueError("a scan is completed by flying it and uploading the result")
        entry = self._log("approve", {"op_id": op_id})
        try:
            self._approve(op_id)
        except Exception:
            self._unlog(entry)
            raise
        return self.ops[op_id].as_dict()

    def reject(self, op_id: str, reason: str = "") -> dict:
        op = self._op(op_id)
        if op.status not in OPEN:
            raise ValueError(f"operation {op_id} is {op.status}")
        self._log("reject", {"op_id": op_id, "reason": reason})
        op.status = "rejected"
        op.reason = f"{op.reason} | rejected: {reason}" if reason else op.reason
        self._event("rejected", f"{op.product} due {op.due} rejected" + (f": {reason}" if reason else ""),
                    op=op_id)
        return op.as_dict()

    def defer(self, op_id: str, days: int) -> dict:
        op = self._op(op_id)
        if op.status not in OPEN:
            raise ValueError(f"operation {op_id} is {op.status}")
        if not 1 <= int(days) <= 14:
            raise ValueError("defer by 1 to 14 days")
        self._log("defer", {"op_id": op_id, "days": int(days)})
        op.due = op.due + dt.timedelta(days=int(days))
        self._event("deferred", f"{op.product} deferred {int(days)} days to {op.due}", op=op_id)
        return op.as_dict()

    def _ensure_flights_phase(self) -> None:
        if self.done:
            raise ValueError("the season is finished")
        if self.phase == "morning":
            self._morning()
            self.phase = "flights"

    def fly_now(self, op_id: str, settings: dict | None = None) -> dict:
        """Fly a proposed or approved job today with optional flight settings (virtual farms)."""
        op = self._op(op_id)
        if not getattr(self.world, "has_truth", True):
            raise ValueError("on a live field, fly with the K3-A Pro and record the flight")
        if op.status not in OPEN or op.product == "scan":
            raise ValueError(f"operation {op_id} is {op.status}")
        if settings is not None:
            MissionSettings(**settings)          # validate before logging
        entry = self._log("fly_now", {"op_id": op_id, "settings": settings})
        try:
            self._ensure_flights_phase()
        except Exception:
            self._unlog(entry)
            raise
        if settings is not None:
            op.settings = dict(settings)
        if op.status == "proposed":
            self._approve(op_id)
        n0 = len(self.events)
        ok = self._fly(self.day, op)
        reason = "" if ok else next((e["text"] for e in reversed(self.events[n0:]) if e["kind"] == "no_fly"),
                                    "no weather record for today")
        return {"flown": ok, "reason": reason, "op": op.as_dict()}

    def scan_now(self, gsd_m: float | None = None, obs=None, ref: str | None = None) -> dict:
        """An extra scan today (virtual farm) or an uploaded scan (``obs``; live field)."""
        if obs is None and not getattr(self.world, "has_truth", True):
            raise ValueError("upload the camera-drone scan for a live field")
        entry = self._log("scan_now", {"gsd_m": gsd_m, "ref": ref})
        try:
            self._ensure_flights_phase()
        except Exception:
            self._unlog(entry)
            raise
        d = self.day
        if obs is None:
            obs = self.world.scan(d, gsd_m or self.scan_gsd_m, self.camera, self.rng, self.archetype.crop)
        herb = bool(self.archetype.weeds) and not any(o.product == "herbicide" and o.status in OPEN
                                                      for o in self.ops.values())
        for o in self.ops.values():
            if o.product == "scan" and o.status in OPEN:
                o.status = "flown"
        self._ingest(d, obs, herbicide=herb, purpose="operator" if ref is None else "upload")
        return {"day": d.isoformat(), "events": self.events[-3:]}

    def finish(self) -> SeasonResult:
        if not self.done:
            raise ValueError("the season is not finished; run it to harvest first")
        h = self.world.harvest_result()
        if h is None:
            raise ValueError("a live field has no simulated harvest: record the measured yield instead")
        econ, st = self.econ, self.st
        rev = econ.revenue(h.field_yield_kg_ha)
        cost_total = sum(self.costs.values())
        decisions = self.twin.field.advice_log() if self.twin is not None else []
        notes = list(self.notes)
        if (self.windy_days and not self.missions_log and not self._leftover
                and not any("no feasible" in n for n in notes)):
            notes.append("no feasible spray window on any due day")
        return SeasonResult(
            policy=self.policy, seed=self.seed, yield_dry_kg_ha=round(h.field_yield_kg_ha, 1),
            yield_marketed_kg_ha=round(h.field_yield_kg_ha / econ.dry_matter, 1), revenue_ha=round(rev, 2),
            costs_ha={k: round(v, 2) for k, v in self.costs.items()}, net_ha=round(rev - cost_total, 2),
            currency=econ.currency, fungicide_sprays=st["fung"], herbicide=st["herb"],
            herbicide_treated_frac=round(st["herb_frac"], 3), missions=len(self.missions_log),
            litres=round(st["litres"], 2), sorties=st["sorties"], battery_swaps=st["swaps"],
            drone_time_min=round(st["time"], 1), off_target_l=round(st["off"], 2),
            mean_cv_pct=round(float(np.mean(self._cvs)), 1) if self._cvs else 0.0,
            mean_skipped_frac=round(float(np.mean(self._skips)), 4) if self._skips else 0.0,
            scans=st["scans"],
            blight_infection_day=h.blight_infection_day.isoformat() if h.blight_infection_day else None,
            final_blight_severity=round(h.blight_final_mean, 4), weed_loss_pct=round(100 * h.weed_loss_mean, 2),
            forecast_sources=dict(self.feed.sources_used), flight=asdict(self.flight), decisions=decisions,
            missions_log=self.missions_log, notes=notes, as_applied_last=self.last_aa)

    def state(self) -> dict:
        st = self.st
        return {"day": self.day.isoformat(), "phase": self.phase, "done": self.done,
                "policy": self.policy, "approvals": self.approvals,
                "das": (self.day - self.world.sowing).days, "sowing": self.world.sowing.isoformat(),
                "harvest": self.world.harvest.isoformat(),
                "ops": [o.as_dict() for o in self.ops.values()],
                "costs": {k: round(v, 2) for k, v in self.costs.items()},
                "missions": len(self.missions_log), "litres": round(st["litres"], 2), "scans": st["scans"],
                "fungicide_sprays": st["fung"], "herbicide": st["herb"], "notes": list(self.notes),
                "n_events": len(self.events), "n_trace": len(self.trace), "flight": asdict(self.flight),
                "scan_das": self.scan_das, "scan_gsd_m": self.scan_gsd_m}

    @classmethod
    def restore(cls, kwargs: dict, actions: list[dict], resolver=None) -> "SeasonSession":
        """Rebuild a session by replaying its logged actions (deterministic)."""
        s = cls(**kwargs)
        for a in actions:
            p = dict(a.get("params") or {})
            if a["action"] == "scan_now" and p.get("ref") is not None:
                if resolver is None:
                    raise ValueError("an uploaded scan needs a resolver to replay")
                p["obs"] = resolver(p["ref"])
            try:
                getattr(s, a["action"])(**p)
            except WaitingForData:
                pass
        return s


def _r(v, nd):
    return None if v is None else round(float(v), nd)
