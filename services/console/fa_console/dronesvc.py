"""The K3-A Pro drone twin over HTTP: the spec sheet (for the 3D model) and a
mission flown over a farm with the full track for playback."""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import yaml

from fa_drone.mission import MissionSettings, Weather, simulate_mission
from fa_drone.spec import DATA as SPEC_DATA, load_spec
from fa_farm.regions import load_region

from .scene import b64, farm_grid


@lru_cache(maxsize=1)
def drone_spec_payload() -> dict:
    s = load_spec()
    raw = yaml.safe_load((SPEC_DATA / "t1_10l.yaml").read_text())
    r = s.wheelbase_mm / 2000.0 * 0.7071
    return {
        "name": s.name, "controller": s.fc.model, "wheelbase_mm": s.wheelbase_mm,
        "unfolded_mm": raw.get("unfolded_mm"), "folded_mm": raw.get("folded_mm"),
        "rotor_diameter_m": s.rotor_diameter_m, "n_rotors": s.n_rotors, "tank_l": s.tank_l,
        "n_nozzles": s.n_nozzles,
        "motors": [{"x": sx * r, "y": sy * r} for sx, sy in ((1, 1), (1, -1), (-1, -1), (-1, 1))],
        "nozzles": [{"x": sx * round(r, 4), "y": sy * round(r, 4)} for sx, sy in ((1, 1), (1, -1), (-1, -1), (-1, 1))],
        "mass_kg": {"empty": s.empty_kg, "dry_with_battery": s.dry_kg, "standard": s.standard_kg, "mtow": s.mtow_kg},
        "hover_points": [list(p) for p in s.hover_points],
        "battery": {"cells": s.battery.cells, "capacity_ah": s.battery.capacity_ah,
                    "nominal_wh": s.battery.nominal_wh, "mass_kg": s.battery.mass_kg,
                    "usable_frac": s.battery.usable_frac, "inferred": s.battery.inferred, "note": s.battery.note},
        "pump_lpm": list(s.pump_lpm), "swath_m": list(s.swath_m), "swath_table_m": s.swath_table_m,
        "charge_min": s.charge_min, "link_range_m": s.link_range_m, "temp_range_c": list(s.temp_range_c),
        "limits": {"work_speed_ms": s.max_work_speed_ms, "speed_ms": s.max_speed_ms,
                   "tilt_deg": s.fc.max_tilt_deg, "vz_ms": s.fc.max_vz_ms, "wind_ms": s.fc.wind_continuous_ms,
                   "gust_ms": s.fc.wind_gust_ms, "waypoints_per_group": s.fc.max_waypoints_per_group,
                   "hover_accuracy_m": s.fc.hover_accuracy_h_m},
        "rtk_optional": s.fc.rtk_optional, "sdk": s.fc.sdk,
        "claimed_acres_per_hour": list(s.claimed_acres_per_hour),
        "notes": ["Spec-sheet inconsistencies are kept as ranges (pump 3.5-5.5 L/min, swath 3-5 m).",
                  "Battery configuration is inferred (2 x 6S 16 Ah) from the two hover points.",
                  "The K3-A Pro has no SDK or telemetry port: missions are exported as KML and job "
                  "records imported after the flight."]}


def simulate(vf, settings: MissionSettings, weather: dict, targets=None, seed: int = 0,
             region: str | None = None, home_xy=None) -> dict:
    grid = farm_grid(vf)
    n = grid.n_cells
    mask = None
    if targets is not None:
        idx = np.asarray(list(targets), dtype=int)
        if idx.size and (idx.min() < 0 or idx.max() >= n):
            raise ValueError(f"target cell index out of range (the farm has {n} cells)")
        mask = np.zeros(n, bool)
        mask[idx] = True
        if not mask.any():
            raise ValueError("no target cells")
    w = Weather(weather["wind_ms"], weather["wind_dir_deg"], weather["gust_ms"], weather["temp_c"],
                elevation_m=float(getattr(vf.spec, "elevation_m", 0.0) or 0.0))
    rules = load_region(region or vf.spec.archetype.region).rules
    res = simulate_mission(grid, load_spec(), settings, w, targets=mask, home_xy=home_xy, seed=seed,
                           rules=rules, record_track=True)
    route = []
    if res.route is not None:
        route = [{"start": [round(p.start[0], 2), round(p.start[1], 2)],
                  "end": [round(p.end[0], 2), round(p.end[1], 2)]} for p in res.route.passes]
    home = [res.track[0]["x"], res.track[0]["y"]] if res.track else None
    return {"feasible": res.feasible, "reasons": res.reasons, "warnings": res.warnings, "kpis": res.kpis,
            "track": res.track, "route": route, "events": res.telemetry, "dose_b64": b64(res.dose_l_ha, "<f2"),
            "home": home, "settings": {k: getattr(res.settings or settings, k)
                                       for k in MissionSettings.__dataclass_fields__},
            "weather": weather, "targets_frac": 1.0 if mask is None else round(float(mask.mean()), 4)}
