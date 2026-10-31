"""The spray drone's specification as data (spec sheet + flight controller)."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DATA = Path(__file__).parent / "data"
G = 9.80665
LIQUID_KG_PER_L = 1.0


@dataclass(frozen=True)
class BatterySpec:
    cells: int
    capacity_ah: float
    nominal_wh: float
    mass_kg: float
    usable_frac: float
    inferred: bool
    note: str = ""


@dataclass(frozen=True)
class FlightControllerSpec:
    model: str
    hover_accuracy_h_m: float
    hover_accuracy_v_m: float
    max_tilt_deg: float
    max_vz_ms: float
    speed_gears_ms: tuple[float, ...]
    max_waypoints_per_group: int
    wind_continuous_ms: float
    wind_gust_ms: float
    rtk_optional: bool
    sdk: bool


@dataclass(frozen=True)
class DroneSpec:
    name: str
    tank_l: float
    n_rotors: int
    n_nozzles: int
    wheelbase_mm: float
    empty_kg: float
    standard_kg: float
    mtow_kg: float
    hover_points: tuple[tuple[float, float], ...]
    rotor_diameter_m: float
    battery: BatterySpec
    pump_lpm: tuple[float, float]
    swath_m: tuple[float, float]
    swath_table_m: float
    max_work_speed_ms: float
    max_speed_ms: float
    link_range_m: float
    temp_range_c: tuple[float, float]
    charge_min: float
    pump_cutoff_ms: float
    fc: FlightControllerSpec
    claimed_acres_per_hour: tuple[float, float] = (10.0, 15.0)

    @property
    def rotor_area_m2(self) -> float:
        import math
        return self.n_rotors * math.pi * (self.rotor_diameter_m / 2) ** 2

    @property
    def dry_kg(self) -> float:
        """Airframe + battery, no liquid."""
        return self.empty_kg + self.battery.mass_kg

    def takeoff_kg(self, liquid_l: float) -> float:
        return self.dry_kg + liquid_l * LIQUID_KG_PER_L

    @property
    def nozzle_offsets_m(self) -> tuple[float, ...]:
        """Lateral nozzle positions (one under each rotor of an X quad)."""
        r = self.wheelbase_mm / 2000.0
        y = r * 0.7071
        return (-y, -y, y, y)


@lru_cache(maxsize=None)
def load_spec(name: str = "t1_10l") -> DroneSpec:
    d = yaml.safe_load((DATA / f"{name}.yaml").read_text())
    b, f = d["battery"], d["flight_controller"]
    return DroneSpec(
        name=d["name"], tank_l=float(d["tank_l"]), n_rotors=int(d["n_rotors"]),
        n_nozzles=int(d["n_nozzles"]), wheelbase_mm=float(d["wheelbase_mm"]),
        empty_kg=float(d["empty_kg"]), standard_kg=float(d["standard_kg"]),
        mtow_kg=float(d["mtow_kg"]),
        hover_points=tuple((float(m), float(t)) for m, t in d["hover_points"]),
        rotor_diameter_m=float(d["rotor_diameter_m"]),
        battery=BatterySpec(int(b["cells"]), float(b["capacity_ah"]), float(b["nominal_wh"]),
                            float(b["mass_kg"]), float(b["usable_frac"]), bool(b["inferred"]),
                            b.get("note", "")),
        pump_lpm=tuple(float(x) for x in d["pump_lpm"]),
        swath_m=tuple(float(x) for x in d["swath_m"]), swath_table_m=float(d["swath_table_m"]),
        max_work_speed_ms=float(d["max_work_speed_ms"]), max_speed_ms=float(d["max_speed_ms"]),
        link_range_m=float(d["link_range_m"]),
        temp_range_c=tuple(float(x) for x in d["temp_range_c"]),
        charge_min=float(d["charge_min"]), pump_cutoff_ms=float(d["pump_cutoff_ms"]),
        fc=FlightControllerSpec(f["model"], float(f["hover_accuracy_h_m"]),
                                float(f["hover_accuracy_v_m"]), float(f["max_tilt_deg"]),
                                float(f["max_vz_ms"]), tuple(float(x) for x in f["speed_gears_ms"]),
                                int(f["max_waypoints_per_group"]), float(f["wind_continuous_ms"]),
                                float(f["wind_gust_ms"]), bool(f["rtk_optional"]), bool(f["sdk"])),
        claimed_acres_per_hour=tuple(float(x) for x in d.get("claimed_acres_per_hour", (10, 15))))
