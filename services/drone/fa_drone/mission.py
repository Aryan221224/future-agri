"""Time-stepped execution of a spray job by the drone twin.

The simulator flies the K3-A Pro route the way the aircraft would: take-off,
ferry, serpentine passes in linkage mode (flow proportional to speed, pump-
capped), turns, return-to-home when the tank is empty or the pack reaches its
return threshold, refill and battery swap on the ground (with pack charging
logistics), and breakpoint resume. Energy comes from the calibrated power model
and the equivalent-circuit battery; position error from the Gauss-Markov GNSS
model; deposition per field cell from the lateral spray pattern along the
*actual* (erroneous) track with the wind's shift and drift.

Outputs are the as-applied dose raster and the KPIs a plan is judged on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .battery import Battery, BatteryCollapse
from .gnss import GnssError
from .k3a import Route, generate_route
from .power import PowerModel
from .spec import LIQUID_KG_PER_L, DroneSpec
from .spray import SprayModel


@dataclass
class MissionSettings:
    height_m: float = 2.5
    speed_ms: float = 5.0
    swath_m: float = 4.0
    rate_l_ha: float = 15.0
    rtk: bool = False
    pump_lpm: float = 3.5
    route_angle_deg: float | None = None
    ferry_speed_ms: float = 8.0
    n_battery_sets: int = 4
    chargers: int = 1
    refill_s: float = 60.0
    battery_swap_s: float = 60.0
    battery_cycles: int = 0


@dataclass
class Weather:
    wind_ms: float
    wind_dir_deg: float          # direction the wind comes FROM (meteorological)
    gust_ms: float
    temp_c: float
    elevation_m: float = 0.0

    @property
    def toward(self) -> tuple[float, float]:
        th = math.radians(self.wind_dir_deg)
        return (-math.sin(th) * self.wind_ms, -math.cos(th) * self.wind_ms)


@dataclass
class MissionResult:
    dose_l_ha: np.ndarray
    kpis: dict
    telemetry: list
    feasible: bool
    reasons: list
    route: Route | None = None
    settings: MissionSettings | None = None
    warnings: list = field(default_factory=list)


def check_conditions(spec: DroneSpec, settings: MissionSettings, weather: Weather,
                     rules=None) -> list[str]:
    r = []
    wmax = spec.fc.wind_continuous_ms if rules is None else min(spec.fc.wind_continuous_ms,
                                                                rules.wind_max_ms)
    if weather.wind_ms > wmax:
        r.append(f"wind {weather.wind_ms:.1f} m/s above limit {wmax:.1f} m/s")
    if weather.gust_ms > spec.fc.wind_gust_ms:
        r.append(f"wind gust {weather.gust_ms:.1f} m/s above the aircraft's {spec.fc.wind_gust_ms} m/s")
    lo, hi = spec.temp_range_c
    if not (lo <= weather.temp_c <= hi):
        r.append(f"temperature {weather.temp_c:.0f} C outside {lo:.0f}..{hi:.0f} C")
    vmax = spec.max_work_speed_ms if rules is None else min(spec.max_work_speed_ms, rules.max_speed_ms)
    if settings.speed_ms > vmax:
        r.append(f"spray speed {settings.speed_ms} m/s above limit {vmax} m/s")
    if settings.speed_ms <= spec.pump_cutoff_ms:
        r.append("spray speed at or below the pump cut-off")
    if rules is not None and settings.height_m > rules.max_height_m:
        r.append(f"height above regulatory maximum {rules.max_height_m} m")
    if settings.pump_lpm > max(spec.pump_lpm):
        r.append(f"pump {settings.pump_lpm} L/min exceeds the installed pump")
    return r


def _infeasible(n: int, reasons: list[str], settings) -> MissionResult:
    kp = {k: 0.0 for k in ("time_min", "flight_min", "energy_wh", "wh_per_ha", "litres", "sorties",
                           "battery_swaps", "refills", "cv_pct", "skipped_frac", "overdosed_frac",
                           "off_target_frac", "area_ha", "ha_per_hour", "waypoints", "home_moves",
                           "max_range_m")}
    return MissionResult(np.zeros(n), kp, [], False, reasons, None, settings)


def simulate_mission(grid, spec: DroneSpec, settings: MissionSettings, weather: Weather,
                     targets: np.ndarray | None = None, home_xy: tuple[float, float] | None = None,
                     seed: int = 0, rules=None, power: PowerModel | None = None,
                     spray: SprayModel | None = None, chunk_m: float = 20.0) -> MissionResult:
    n = grid.n_cells
    reasons = check_conditions(spec, settings, weather, rules)
    if reasons:
        return _infeasible(n, reasons, settings)
    route = generate_route(grid, settings, spec, targets)
    if not route.passes:
        return _infeasible(n, ["no spray pass fits the treated area at this spacing"], settings)

    pm = (power or PowerModel.calibrate(spec)).with_air(weather.temp_c, weather.elevation_m)
    sm = spray or SprayModel(spec)
    gnss = GnssError(settings.rtk, seed=seed)
    v, W, h = settings.speed_ms, settings.swath_m, settings.height_m
    rate_eff = sm.achieved_rate(v, settings.rate_l_ha, W, settings.pump_lpm)
    l_per_m = rate_eff * W / 10_000.0
    wind = weather.toward
    warn = []
    if rate_eff < settings.rate_l_ha * 0.999:
        warn.append(f"pump-limited: {rate_eff:.1f} of {settings.rate_l_ha:.1f} L/ha at {v} m/s")

    first = route.passes[0].start
    home = home_xy or tuple(np.array(first) + 5.0 * np.array(route.passes[0].normal) * -1)
    batt = Battery(spec, temp_c=weather.temp_c, cycles=settings.battery_cycles)
    pack_ready = [0.0] * max(0, settings.n_battery_sets - 1)
    charger_free = [0.0] * max(1, settings.chargers)
    st = dict(t=0.0, flight=0.0, energy=0.0, liquid=spec.tank_l, sorties=1, refills=0, swaps=0,
              sprayed=0.0, moves=0, max_range=0.0)
    home_ref = [home]
    tel: list[dict] = []
    samples: dict[int, list] = {}

    def mass():
        return spec.dry_kg + st["liquid"] * LIQUID_KG_PER_L

    def fly(power_w: float, dur: float, gnss_log: list | None = None):
        rem = dur
        while rem > 1e-9:
            dt = min(1.0, rem)
            try:
                batt.draw(power_w, dt)
            except BatteryCollapse:
                batt.last_v = 0.0
            ex, ey = gnss.step(dt)
            if gnss_log is not None:
                gnss_log.append((dt, ex, ey))
            st["t"] += dt
            st["flight"] += dt
            st["energy"] += power_w * dt / 3600.0
            rem -= dt

    def ferry(a, b):
        d = math.dist(a, b)
        if d < 0.1:
            return
        sp = settings.ferry_speed_ms if d > 15 else 2.0
        ux, uy = (b[0] - a[0]) / d, (b[1] - a[1]) / d
        head = -(wind[0] * ux + wind[1] * uy)
        fly(pm.forward_w(mass(), sp, head), d / sp)

    def vertical(height):
        fly(pm.hover_w(mass()) * 1.1, height / 2.0)

    def e_return(pos) -> float:
        d = math.dist(pos, home_ref[0])
        return (pm.forward_w(mass(), settings.ferry_speed_ms) * d / settings.ferry_speed_ms
                + pm.hover_w(mass()) * 1.1 * (h / 2.0)) / 3600.0

    def needs_rth(pos, p_spray) -> str | None:
        if st["liquid"] <= 1e-6:
            return "tank empty"
        if batt.low_voltage() or batt.usable_wh_left(p_spray) < 1.3 * e_return(pos) + 5.0:
            return "battery"
        return None

    def ground_ops(reason: str):
        # land, refill to full, swap the pack if it cannot fly another full tank
        tg = 0.0
        if st["liquid"] < spec.tank_l - 0.5:
            tg += settings.refill_s * (spec.tank_l - st["liquid"]) / spec.tank_l
            st["liquid"] = spec.tank_l
            st["refills"] += 1
        p_spray = pm.forward_w(mass(), v)
        e_tank = spec.tank_l / max(l_per_m, 1e-9) / v * p_spray / 3600.0
        if reason == "battery" or batt.usable_wh_left(p_spray) < 1.2 * (e_tank + 2 * e_return(first)):
            # swap: the used pack goes on charge, take the earliest ready pack
            now = st["t"] + tg
            k = int(np.argmin(charger_free))
            start = max(now, charger_free[k])
            charger_free[k] = start + spec.charge_min * 60.0
            pack_ready.append(charger_free[k])
            pack_ready.sort()
            ready = pack_ready.pop(0)
            tg = max(tg, settings.battery_swap_s, ready - st["t"])
            batt.__init__(spec, temp_c=weather.temp_c, cycles=settings.battery_cycles)
            st["swaps"] += 1
        st["t"] += tg
        tel.append({"t_s": round(st["t"], 1), "event": "ground", "reason": reason,
                    "ground_s": round(tg, 1)})

    def rth_and_return(pos, reason):
        tel.append({"t_s": round(st["t"], 1), "event": "rth", "reason": reason,
                    "soc": round(batt.soc, 3), "liquid_l": round(st["liquid"], 2)})
        ferry(pos, home_ref[0])
        vertical(h)
        ground_ops(reason)
        st["sorties"] += 1
        vertical(h)
        ferry(home_ref[0], pos)
        tel.append({"t_s": round(st["t"], 1), "event": "resume"})

    # ---- fly the job
    link = 0.9 * spec.link_range_m
    relocate_s = 600.0                  # crew moves the take-off point (assumption: 10 min)
    vertical(h)
    pos = home
    for pi, ps in enumerate(route.passes):
        far = max(math.dist(home_ref[0], ps.start), math.dist(home_ref[0], ps.end))
        if far > link:
            # beyond the K3-A Pro radio range: land, move the crew to this pass, take off again
            ferry(pos, home_ref[0])
            vertical(h)
            ux0, uy0 = ps.unit
            nx0, ny0 = ps.normal
            mx, my = (ps.start[0] + ps.end[0]) / 2, (ps.start[1] + ps.end[1]) / 2
            home_ref[0] = (mx - 5.0 * nx0, my - 5.0 * ny0)     # beside the middle of the pass
            st["t"] += relocate_s
            st["moves"] += 1
            tel.append({"t_s": round(st["t"], 1), "event": "relocate_home",
                        "reason": f"route beyond {spec.link_range_m:.0f} m link range"})
            vertical(h)
            pos = home_ref[0]
            st["sorties"] += 1
        st["max_range"] = max(st["max_range"], math.dist(home_ref[0], ps.start),
                              math.dist(home_ref[0], ps.end))
        ferry(pos, ps.start)
        pos = ps.start
        ux, uy = ps.unit
        head = -(wind[0] * ux + wind[1] * uy)
        s, L = 0.0, ps.length
        samples[pi] = []
        while s < L - 1e-6:
            p_spray = pm.forward_w(mass(), v, head)
            why = needs_rth(pos, p_spray)
            if why:
                rth_and_return(pos, why)
                continue
            chunk = min(chunk_m, L - s, st["liquid"] / max(l_per_m, 1e-9))
            log: list = []
            fly(p_spray, chunk / v, log)
            # gnss samples along the chunk -> (s, ex, ey)
            ss = s
            for dt, ex, ey in log:
                ss += v * dt
                samples[pi].append((min(ss, L), ex, ey))
            st["liquid"] = max(0.0, st["liquid"] - chunk * l_per_m)
            st["sprayed"] += chunk * l_per_m
            s += chunk
            pos = (ps.start[0] + ux * s, ps.start[1] + uy * s)
        # turn: decelerate, shift, accelerate (no yaw: nozzles are symmetric)
        a = 9.80665 * math.tan(math.radians(spec.fc.max_tilt_deg))
        fly(pm.hover_w(mass()) * 1.05, 2 * v / a + W / 2.0)
    ferry(pos, home_ref[0])
    vertical(h)
    tel.append({"t_s": round(st["t"], 1), "event": "landed", "soc": round(batt.soc, 3)})

    # ---- deposition on the field grid along the actual track
    dose = np.zeros(n)
    xy = grid.cells_xy
    wrng = np.random.default_rng(seed + 7919)
    for pi, ps in enumerate(route.passes):
        smp = samples.get(pi) or []
        if not smp:
            continue
        sp = np.array(smp)
        nx_, ny_ = ps.normal
        ux, uy = ps.unit
        e_ct = sp[:, 1] * nx_ + sp[:, 2] * ny_
        s_done = sp[:, 0].max()
        wind_cross = sm.pass_wind(wind[0] * nx_ + wind[1] * ny_, wrng)
        reach = 3 * sm.sigma(h, weather.wind_ms) + abs(sm.shift(h, wind_cross)) + 3.0
        rel = xy - np.array(ps.start)
        s_cell = rel[:, 0] * ux + rel[:, 1] * uy
        d_cell = rel[:, 0] * nx_ + rel[:, 1] * ny_
        near = (np.abs(d_cell) < reach) & (s_cell >= -grid.cell_m / 2) & (s_cell <= s_done + grid.cell_m / 2)
        if not near.any():
            continue
        e_at = np.interp(np.clip(s_cell[near], 0, s_done), sp[:, 0], e_ct)
        dens = sm.lateral_density(d_cell[near] - e_at, h, wind_cross, cell_w=grid.cell_m)
        dose[near] += rate_eff * W * dens

    tmask = np.ones(n, bool) if targets is None else np.asarray(targets, bool)
    area = tmask.sum() * grid.cell_m ** 2 / 1e4
    deposited = dose.sum() * grid.cell_m ** 2 / 1e4
    litres = st["sprayed"]
    dt_ = dose[tmask] if tmask.any() else dose
    rate = settings.rate_l_ha
    kpis = {
        "time_min": round(st["t"] / 60, 2), "flight_min": round(st["flight"] / 60, 2),
        "energy_wh": round(st["energy"], 1), "wh_per_ha": round(st["energy"] / max(area, 1e-9), 1),
        "litres": round(litres, 2), "sorties": int(st["sorties"]), "battery_swaps": int(st["swaps"]),
        "refills": int(st["refills"]),
        "cv_pct": round(100 * float(dt_.std() / dt_.mean()) if dt_.mean() > 0 else 100.0, 1),
        "skipped_frac": round(float((dt_ < 0.5 * rate).mean()), 4),
        "overdosed_frac": round(float((dt_ > 1.5 * rate).mean()), 4),
        "off_target_frac": round(max(0.0, 1 - deposited / litres) if litres > 0 else 0.0, 4),
        "area_ha": round(area, 3), "ha_per_hour": round(area / max(st["t"] / 3600, 1e-9), 2),
        "waypoints": route.n_waypoints, "route_groups": len(route.groups),
        "home_moves": int(st["moves"]), "max_range_m": round(st["max_range"], 1),
        "achieved_rate_l_ha": round(rate_eff, 2), "mean_dose_l_ha": round(float(dt_.mean()), 2),
    }
    return MissionResult(dose, kpis, tel, True, [], route, settings, warn)
