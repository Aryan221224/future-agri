"""The virtual pilot: flies a spray job on the K3-A Pro through the S-BUS bridge.

Call ``tick(now, fix, power, bridge)`` 20 times a second; it returns the 16 S-BUS channel
values to send (or None to send nothing, which makes the bridge hold and then return home).

    IDLE -> PREFLIGHT -> ARMING -> TAKEOFF -> TRANSIT -> ALIGN -> PASS -> ... -> RETURN
         -> LAND -> LANDED (battery swap) -> PREFLIGHT ... -> DONE
    any airborne state -> HOLD (pause, RTK float, off track, pilot hand-back)
                       -> RETURN (abort, geofence, battery low, hold timeout)
                       -> LAND_NOW (battery critical)
                       -> RTH_K3 (position or heading lost: the K3-A Pro's own return home)
                       -> PILOT (the pilot switched to manual: hands off)

Safety rules held in every state:
* throttle low (fast descent in GPS-SPEED) is only ever sent on the ground;
* the pump is on only in PASS, on the line, at spray height;
* every transition is logged with its reason.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .follower import Gains, LegFollower, heading_of, to_body, wrap180, yaw_rate
from .profile import K3Profile
from .route import Job, Pass

AIRBORNE = {"TAKEOFF", "TRANSIT", "ALIGN", "PASS", "HOLD", "RETURN", "LAND", "LAND_NOW", "CALIBRATE"}


@dataclass
class PilotConfig:
    tick_hz: float = 20.0
    cell_full_v: float = 3.85          # per cell at the start of a sortie
    cell_return_v: float = 3.45        # under load: return home
    cell_land_v: float = 3.30          # under load: land where it is
    cells: int = 12
    home_radius_m: float = 15.0        # take off within this of home
    fix_timeout_s: float = 1.0
    float_hold_s: float = 15.0         # RTK float this long in HOLD -> return home
    hold_timeout_s: float = 120.0      # paused this long -> return home
    bridge_timeout_s: float = 1.5
    xtrack_hold_m: float = 3.0
    xtrack_return_m: float = 10.0
    xtrack_window_s: float = 5.0
    lead_in_m: float = 4.0
    takeoff_timeout_s: float = 8.0
    arm_check: str = "current"         # "current" (power monitor) | "none"
    arm_confirm_s: float = 3.0
    land_descent_ms: float = 0.7
    land_slow_ms: float = 0.35
    pos_kp: float = 0.8
    pos_vmax: float = 1.5
    align_heading_deg: float = 6.0
    align_height_m: float = 0.3


@dataclass
class Event:
    t: float
    state: str
    reason: str


@dataclass
class Pilot:
    profile: K3Profile
    cfg: PilotConfig = field(default_factory=PilotConfig)
    gains: Gains = field(default_factory=Gains)
    state: str = "IDLE"
    job: Job | None = None
    sortie: int = 0
    pass_i: int = 0
    events: list[Event] = field(default_factory=list)
    preflight: list[str] = field(default_factory=list)

    def __post_init__(self):
        self._t = 0.0
        self._entered = 0.0
        self._follower: LegFollower | None = None
        self._hold_at: tuple[float, float, float] | None = None
        self._after_hold: str | None = None
        self._xt_bad_since: float | None = None
        self._batt_low_since: float | None = None
        self._last_fix_t = -1e9
        self._resume_point: tuple[float, float] | None = None
        self._arm_step = 0
        self._arm_step_t = 0.0
        self._battery_swapped = False
        self._abort_reason: str | None = None
        self._land_still = 0.0
        self._ground_since = None
        self.spraying = False
        self.xtrack: float | None = None

    # ---------------------------------------------------------------- commands
    def start(self, job: Job) -> None:
        if self.state not in ("IDLE", "DONE", "FAILED"):
            raise RuntimeError(f"cannot start a job while {self.state}")
        self.job, self.sortie, self.pass_i = job, 0, 0
        self._go("PREFLIGHT", f"job {job.job_id}: {len(job.sorties)} sorties")

    def start_calibration(self, job: Job) -> None:
        """Take off at home, fly the calibration steps (fa_agent.calibrate), return and land.
        ``job`` gives the fence and heights; its passes are not flown."""
        from .calibrate import Calibrator
        if self.state not in ("IDLE", "DONE", "FAILED"):
            raise RuntimeError(f"cannot calibrate while {self.state}")
        self.job, self.sortie, self.pass_i = job, 0, 0
        self.calibrator = Calibrator(self.profile)
        self.calibration: dict | None = None
        self._go("PREFLIGHT", "calibration flight")

    def abort(self, reason: str = "operator abort") -> None:
        if self.state in AIRBORNE and self.state not in ("RETURN", "LAND", "LAND_NOW"):
            self._go("RETURN", reason)
        elif self.state in ("PREFLIGHT", "ARMING"):
            self._go("IDLE", reason)
        elif self.state == "LANDED":
            self._go("DONE", reason)

    def pause(self) -> None:
        if self.state in ("TRANSIT", "ALIGN", "PASS", "RETURN"):
            self._hold("operator pause")

    def resume(self) -> None:
        if self.state == "HOLD":
            self._go(self._after_hold or "RETURN", "operator resume")
            self._replan_from_here()

    def battery_swapped(self) -> None:
        self._battery_swapped = True

    # ---------------------------------------------------------------- loop
    def tick(self, now: float, fix, power, bridge) -> list[int] | None:
        self._t = now
        if fix is not None and fix.kind != "none":
            self._last_fix_t = fix.t
        st = self.state
        if st in AIRBORNE or st in ("RTH_K3", "PILOT"):
            self._airborne_checks(fix, power, bridge)
        handler = getattr(self, "_s_" + self.state.lower())
        return handler(fix, power, bridge)

    # ---------------------------------------------------------------- checks
    def _airborne_checks(self, fix, power, bridge) -> None:
        c = self.cfg
        if bridge is not None and bridge.source == "pilot" and self.state != "PILOT":
            self._go("PILOT", "the pilot switched to manual: hands off")
            return
        if self.state == "PILOT":
            if bridge is not None and bridge.source in ("companion", "hold", "rth") and bridge.auto_allowed:
                self._hold("pilot handed back: holding for the operator to resume")
            return
        if self._on_ground_disarmed(fix, power):
            self._go("LANDED", "the aircraft is on the ground with its motors stopped")
            return
        if self.state == "RTH_K3":
            return
        fix_age = self._t - self._last_fix_t
        if fix is None or fix_age > c.fix_timeout_s or fix.kind in ("none", "3d") or not fix.heading_ok:
            why = "no position" if (fix is None or fix_age > c.fix_timeout_s or fix.kind == "none") else (
                "no RTK (3D fix only)" if fix.kind == "3d" else "no heading")
            self._go("RTH_K3", f"{why}: the K3-A Pro returns home by itself")
            return
        if fix.kind == "float" and self.state in ("TRANSIT", "ALIGN", "PASS"):
            self._hold("RTK float: paused until fixed")
            return
        if self.job is not None and not self.job.inside_fence((fix.e, fix.n)) and self.state not in ("RETURN", "LAND",
                                                                                                    "LAND_NOW"):
            self._go("RETURN", "left the geofence")
            return
        if self.job is not None and fix.h > self.job.ceiling_m:
            self._go("RETURN", "above the ceiling")
            return
        if power is not None:
            cell = power.voltage / c.cells
            if cell < c.cell_land_v and self.state != "LAND_NOW":
                self._go("LAND_NOW", f"battery critical ({cell:.2f} V/cell): landing here")
                return
            if cell < c.cell_return_v:
                self._batt_low_since = self._batt_low_since if self._batt_low_since is not None else self._t
                if self._t - self._batt_low_since > 3.0 and self.state in ("TRANSIT", "ALIGN", "PASS", "HOLD"):
                    self._go("RETURN", f"battery low ({cell:.2f} V/cell)")
                    return
            else:
                self._batt_low_since = None
        if bridge is not None and self._t - bridge.at > c.bridge_timeout_s:
            self._log("bridge status stale: if the bridge is down the pilot receiver is connected directly")
        if self.state == "PASS" and self.xtrack is not None:
            if abs(self.xtrack) > c.xtrack_return_m:
                self._go("RETURN", f"{abs(self.xtrack):.1f} m off the line")
            elif abs(self.xtrack) > c.xtrack_hold_m:
                self._xt_bad_since = self._xt_bad_since if self._xt_bad_since is not None else self._t
                if self._t - self._xt_bad_since > c.xtrack_window_s:
                    self._hold(f"{abs(self.xtrack):.1f} m off the line for {c.xtrack_window_s:.0f} s")
            else:
                self._xt_bad_since = None

    def _on_ground_disarmed(self, fix, power) -> bool:
        """Landed by someone else (the pilot, the K3-A Pro's own return home): motors stopped,
        no height, not moving, for 3 s. Never while taking off (the motors spin up first)."""
        if self.state == "TAKEOFF" or power is None or fix is None:
            self._ground_since = None
            return False
        still = fix.h < 0.3 and math.hypot(fix.v_e, fix.v_n) < 0.2 and abs(fix.v_u) < 0.2
        if still and power.current < self.profile.idle_current_a * 0.5:
            self._ground_since = self._ground_since if self._ground_since is not None else self._t
            return self._t - self._ground_since > 3.0
        self._ground_since = None
        return False

    # ---------------------------------------------------------------- states on the ground
    def _ground(self) -> list[int]:
        us = self.profile.neutral()
        us[self.profile.throttle.ch] = 1000          # throttle low: only ever on the ground
        return us

    def _s_idle(self, fix, power, bridge):
        return self._ground()

    _s_done = _s_idle
    _s_failed = _s_idle

    def _s_preflight(self, fix, power, bridge):
        c, why = self.cfg, []
        if bridge is None or self._t - bridge.at > c.bridge_timeout_s:
            why.append("no status from the S-BUS bridge")
        elif not bridge.auto_allowed:
            why.append("the pilot's AUTO switch is off")
        elif not bridge.pilot_ok:
            why.append("no signal from the pilot's receiver")
        if fix is None or fix.kind != "fixed":
            why.append(f"RTK not fixed ({fix.kind if fix else 'no fix'})")
        elif not fix.heading_ok:
            why.append("no RTK heading")
        elif math.hypot(fix.e, fix.n) > c.home_radius_m:
            why.append(f"{math.hypot(fix.e, fix.n):.0f} m from home (max {c.home_radius_m:.0f} m)")
        if power is None and c.arm_check == "current":
            why.append("no reading from the power monitor")
        elif power is not None and power.voltage / c.cells < c.cell_full_v:
            why.append(f"battery {power.voltage / c.cells:.2f} V/cell (needs {c.cell_full_v:.2f})")
        self.preflight = why
        if why:
            if self._t - self._entered > 10.0:
                self._go("IDLE" if self.sortie == 0 else "LANDED", "preflight failed: " + "; ".join(why))
            return self._ground()
        self._arm_step, self._arm_step_t = 0, self._t
        self._go("ARMING", "preflight passed")
        return self._ground()

    def _s_arming(self, fix, power, bridge):
        seq = self.profile.arm_sequence
        if self._arm_step < len(seq):
            ms, chs = seq[self._arm_step]
            if (self._t - self._arm_step_t) * 1000 >= ms:
                self._arm_step += 1
                self._arm_step_t = self._t
            else:
                us = self._ground()
                for ch, v in chs.items():
                    us[int(ch)] = int(v)
                return us
        # sticks released: confirm the motors are spinning
        if self.cfg.arm_check == "none" or (power is not None and power.current >= self.profile.idle_current_a):
            self._hold_at = (fix.e, fix.n, 0.0) if fix else (0.0, 0.0, 0.0)
            self._go("TAKEOFF", "armed")
            return self._ground()
        if self._t - self._arm_step_t > self.cfg.arm_confirm_s:
            self._go("FAILED", "arming not confirmed (no motor current): check the arm sequence on the bench")
        return self._ground()

    def _s_landed(self, fix, power, bridge):
        if getattr(self, "calibration", None) is not None:
            self._go("DONE", "calibration flight complete")
            return self._ground()
        if self.job is None or self.sortie + 1 >= len(self.job.sorties):
            self._go("DONE", "job complete")
            return self._ground()
        if self._battery_swapped:
            self._battery_swapped = False
            self.sortie += 1
            self.pass_i = 0
            self._go("PREFLIGHT", f"battery swapped: sortie {self.sortie + 1}")
        return self._ground()

    # ---------------------------------------------------------------- airborne states
    def _hover(self, fix, target, height=None) -> list[int]:
        e, n, h = target
        ve = max(-self.cfg.pos_vmax, min(self.cfg.pos_vmax, self.cfg.pos_kp * (e - fix.e)))
        vn = max(-self.cfg.pos_vmax, min(self.cfg.pos_vmax, self.cfg.pos_kp * (n - fix.n)))
        climb = max(-self.gains.climb_max, min(self.gains.climb_max, self.gains.kz * ((height if height is not None
                                                                                        else h) - fix.h)))
        return self._cmd(fix, ve, vn, climb, fix.heading, pump=False)

    def _cmd(self, fix, ve, vn, climb, heading, pump: bool) -> list[int]:
        fwd, right = to_body(ve, vn, fix.heading)
        self.spraying = pump
        return self.profile.command(fwd, right, climb, yaw_rate(heading, fix.heading, self.gains), pump)

    def _s_takeoff(self, fix, power, bridge):
        target_h = self.job.transit_height_m
        if fix.h >= target_h - 0.3:
            if getattr(self, "calibrator", None) is not None:
                self._go("CALIBRATE", "calibration steps")
                return self._hover(fix, (*self._hold_at[:2], target_h))
            self._start_transit()
            return self._hover(fix, (*self._hold_at[:2], target_h))
        if self._t - self._entered > self.cfg.takeoff_timeout_s and fix.h < 0.3:
            self._go("LAND", "no lift-off: landing to disarm")
        us = self._hover(fix, (*self._hold_at[:2], target_h))
        return us

    def _passes(self) -> list[Pass]:
        return self.job.sorties[self.sortie]

    def _start_transit(self) -> None:
        if self.pass_i >= len(self._passes()):
            self._go("RETURN", f"sortie {self.sortie + 1} done")
            return
        p = self._passes()[self.pass_i]
        (ax, ay), (bx, by) = p.points[0], p.points[1]
        d = math.hypot(bx - ax, by - ay) or 1.0
        pre = (ax - self.cfg.lead_in_m * (bx - ax) / d, ay - self.cfg.lead_in_m * (by - ay) / d)
        self._target = pre
        self._go("TRANSIT", f"to pass {self.pass_i + 1} of sortie {self.sortie + 1}")
        self._follower = None

    def _s_transit(self, fix, power, bridge):
        j = self.job
        far = math.dist((fix.e, fix.n), self._target) > 30.0
        h = j.transit_height_m if far or fix.h > j.height_m + 1.0 and math.dist((fix.e, fix.n), self._target) > 8 \
            else j.height_m
        if self._follower is None:
            self._follower = LegFollower([(fix.e, fix.n), self._target], j.transit_speed_ms, h,
                                         spray=False, gains=self.gains)
        self._follower.height = h
        sp = self._follower.update(fix.e, fix.n, fix.v_e, fix.v_n, fix.h, 1 / self.cfg.tick_hz)
        if sp.done:
            self._hold_at = (*self._target, j.height_m)
            self._go("ALIGN", f"at the start of pass {self.pass_i + 1}")
            return self._hover(fix, self._hold_at)
        move_heading = heading_of(self._target[0] - fix.e, self._target[1] - fix.n) if far else self._pass_heading()
        return self._cmd(fix, sp.v_e, sp.v_n, sp.climb, move_heading, pump=False)

    def _pass_heading(self) -> float:
        p = self._passes()[self.pass_i].points
        return heading_of(p[1][0] - p[0][0], p[1][1] - p[0][1])

    def _s_align(self, fix, power, bridge):
        hd = self._pass_heading()
        us = self._hover(fix, self._hold_at)
        self.spraying = False
        fwd, right = to_body(0, 0, fix.heading)
        us = self.profile.command(*to_body(
            max(-1.5, min(1.5, self.cfg.pos_kp * (self._hold_at[0] - fix.e))),
            max(-1.5, min(1.5, self.cfg.pos_kp * (self._hold_at[1] - fix.n))), fix.heading),
            max(-1.5, min(1.5, self.gains.kz * (self.job.height_m - fix.h))),
            yaw_rate(hd, fix.heading, self.gains), False)
        if abs(wrap180(hd - fix.heading)) < self.cfg.align_heading_deg and abs(fix.h - self.job.height_m) < \
                self.cfg.align_height_m and math.dist((fix.e, fix.n), self._hold_at[:2]) < 0.6:
            p = self._passes()[self.pass_i]
            pts = [self._hold_at[:2]] + list(p.points) if self._resume_point is None else \
                [(fix.e, fix.n)] + [self._resume_point] + self._resume_rest
            self._resume_point = None
            self._follower = LegFollower(pts, self.job.speed_ms, self.job.height_m, spray=p.spray, gains=self.gains)
            self._go("PASS", f"pass {self.pass_i + 1} of sortie {self.sortie + 1}")
        return us

    def _s_pass(self, fix, power, bridge):
        f = self._follower
        sp = f.update(fix.e, fix.n, fix.v_e, fix.v_n, fix.h, 1 / self.cfg.tick_hz)
        self.xtrack = sp.xtrack
        if sp.done:
            self.xtrack = None
            self.pass_i += 1
            self._start_transit()
            return self._hover(fix, (fix.e, fix.n, self.job.height_m))
        spray = sp.spray and f.leg >= 1                    # never on the lead-in
        return self._cmd(fix, sp.v_e, sp.v_n, sp.climb, sp.heading, pump=spray)

    def _hold(self, reason: str) -> None:
        if self.state == "HOLD":
            return
        self._after_hold = self.state if self.state in ("TRANSIT", "ALIGN", "PASS", "RETURN") else "RETURN"
        self._hold_point = None
        self._go("HOLD", reason)

    def _s_hold(self, fix, power, bridge):
        if self._hold_point is None:
            self._hold_point = (fix.e, fix.n, fix.h)
        reason = self.events[-1].reason if self.events else ""
        if fix.kind == "fixed" and reason.startswith("RTK float"):
            self._go(self._after_hold, "RTK fixed again: resuming")
            self._replan_from_here()
        elif fix.kind == "float" and self._t - self._entered > self.cfg.float_hold_s:
            self._go("RETURN", f"RTK float for {self.cfg.float_hold_s:.0f} s")
        elif self._t - self._entered > self.cfg.hold_timeout_s:
            self._go("RETURN", "held too long")
        return self._hover(fix, self._hold_point)

    def _replan_from_here(self) -> None:
        """Resume the interrupted pass from the point the aircraft is nearest to, not the start."""
        if self.state in ("PASS", "ALIGN") and self._follower is not None and self.job is not None:
            f = self._follower
            pts = f.points
            k = min(f.leg + 1, len(pts) - 1)
            (ax, ay), (bx, by) = pts[k - 1], pts[k]
            hp = self._hold_point or (ax, ay, 0)
            L = math.dist((ax, ay), (bx, by)) or 1.0
            s = max(0.0, min(L, ((hp[0] - ax) * (bx - ax) + (hp[1] - ay) * (by - ay)) / L))
            self._resume_point = (ax + s * (bx - ax) / L, ay + s * (by - ay) / L)
            self._resume_rest = list(pts[k:])
            back = (self._resume_point[0] - self.cfg.lead_in_m * (bx - ax) / L,
                    self._resume_point[1] - self.cfg.lead_in_m * (by - ay) / L)
            self._target = back
            self._hold_at = (*back, self.job.height_m)
            self.state = "TRANSIT"
            self._follower = None
        elif self.state == "TRANSIT":
            self._follower = None

    def _s_return(self, fix, power, bridge):
        j = self.job
        d = math.hypot(fix.e, fix.n)
        if d < 0.8:
            self._go("LAND", "over home")
            return self._hover(fix, (0.0, 0.0, fix.h))
        if fix.h < j.transit_height_m - 0.5 and d > 5.0:
            return self._hover(fix, (fix.e, fix.n, j.transit_height_m))   # climb first, then go
        if self._follower is None or getattr(self._follower, "_home", False) is False:
            self._follower = LegFollower([(fix.e, fix.n), (0.0, 0.0)], j.transit_speed_ms, max(fix.h,
                                         j.transit_height_m if d > 5 else fix.h), spray=False, gains=self.gains)
            self._follower._home = True
        sp = self._follower.update(fix.e, fix.n, fix.v_e, fix.v_n, fix.h, 1 / self.cfg.tick_hz)
        return self._cmd(fix, sp.v_e, sp.v_n, sp.climb, heading_of(-fix.e, -fix.n) if d > 3 else fix.heading,
                         pump=False)

    def _s_calibrate(self, fix, power, bridge):
        us = self.calibrator.command(self._t, fix)
        if us is None:
            self.calibration = self.calibrator.report
            self.calibrator = None
            self._go("RETURN", "calibration done: " + (", ".join(self.calibration["applied"]) or "nothing applied"))
            return self._hover(fix, (fix.e, fix.n, fix.h))
        self.spraying = False
        return us

    def _s_land(self, fix, power, bridge):
        return self._descend(fix, power, (0.0, 0.0))

    def _s_land_now(self, fix, power, bridge):
        if self._hold_at is None or self.events[-1].state != "LAND_NOW":
            pass
        return self._descend(fix, power, (fix.e, fix.n) if self._t - self._entered < 0.06 else self._land_xy)

    def _descend(self, fix, power, xy) -> list[int]:
        if not hasattr(self, "_land_xy") or self._t - self._entered < 0.06:
            self._land_xy = xy
        e, n = self._land_xy
        rate = self.cfg.land_descent_ms if fix.h > 1.5 else self.cfg.land_slow_ms
        ve = max(-1.0, min(1.0, self.cfg.pos_kp * (e - fix.e)))
        vn = max(-1.0, min(1.0, self.cfg.pos_kp * (n - fix.n)))
        on_ground = fix.h < 0.15 and abs(fix.v_u) < 0.1
        self._land_still = self._land_still + 1 / self.cfg.tick_hz if on_ground else 0.0
        stopped = power is not None and power.current < self.profile.idle_current_a * 0.5
        if self._land_still > 1.5 and (stopped or self.cfg.arm_check == "none" or self._land_still > 6.0):
            self._go("LANDED", f"landed after sortie {self.sortie + 1}")
            return self._ground()
        if self._land_still > 1.0:
            return self._ground()                         # on the ground: throttle low lets it disarm
        fwd, right = to_body(ve, vn, fix.heading)
        self.spraying = False
        return self.profile.command(fwd, right, -rate, 0.0, False)

    def _s_rth_k3(self, fix, power, bridge):
        us = self.profile.neutral()
        us[self.profile.rth_ch] = self.profile.rth_on_us
        self.spraying = False
        if fix is not None and fix.kind == "fixed" and fix.heading_ok and fix.h < 0.15 and abs(fix.v_u) < 0.1:
            self._land_still += 1 / self.cfg.tick_hz
            if self._land_still > 3.0:
                self._go("LANDED", "the K3-A Pro returned home and landed")
        return us

    def _s_pilot(self, fix, power, bridge):
        self.spraying = False
        return self._hover(fix, (fix.e, fix.n, fix.h)) if fix is not None else self.profile.neutral()

    # ---------------------------------------------------------------- log
    def _go(self, state: str, reason: str) -> None:
        self.state = state
        self._entered = self._t
        self._land_still = 0.0
        if state not in ("TRANSIT", "PASS"):
            self.xtrack = None
        self.events.append(Event(round(self._t, 2), state, reason))

    def _log(self, reason: str) -> None:
        if not self.events or self.events[-1].reason != reason:
            self.events.append(Event(round(self._t, 2), self.state, reason))
