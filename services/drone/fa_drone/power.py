"""Multirotor power from momentum theory, calibrated to the spec sheet's two
hover points (12 min at 24 kg, 28 min at 14 kg).

    P_ideal = (m g)^1.5 / sqrt(2 rho A)                 (ideal induced power, hover)
    P_elec  = (P_induced + P_profile + P_parasitic) / eta_drive + P_avionics

The two hover points fix the overall efficiency eta = FM x eta_drive. Their
consistency is the check: momentum theory predicts endurance scaling with
m^-1.5 and the sheet implies m^-1.57, so one eta reproduces both points within
~2 %. Forward flight adds Glauert induced velocity, profile power growing with
advance ratio, and parasitic drag on the airspeed (wind included).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .spec import G, DroneSpec

RHO0 = 1.225


def air_density(temp_c: float = 15.0, elevation_m: float = 0.0) -> float:
    p = 101325.0 * (1 - 2.25577e-5 * elevation_m) ** 5.25588
    return p / (287.05 * (temp_c + 273.15))


@dataclass
class PowerModel:
    rotor_area_m2: float
    eta: float                     # overall hover efficiency (FM x drive)
    eta_drive: float = 0.85        # motor + ESC (assumption)
    p_avionics_w: float = 30.0     # FC, GNSS, radio, LEDs (assumption)
    cda_m2: float = 0.45           # frame + tank drag area (assumption, calibrate from logs)
    tip_speed_ms: float = 185.0    # 34.7 in prop, ~4,000 rpm loaded
    usable_wh: float = 603.8
    rho: float = RHO0

    @property
    def figure_of_merit(self) -> float:
        return self.eta / self.eta_drive

    @classmethod
    def calibrate(cls, spec: DroneSpec, eta_drive: float = 0.85, p_avionics_w: float = 30.0,
                  cda_m2: float = 0.45) -> "PowerModel":
        usable = spec.battery.nominal_wh * spec.battery.usable_frac
        A = spec.rotor_area_m2
        etas = []
        for m, t_min in spec.hover_points:
            p_elec = usable / (t_min / 60.0)
            p_ideal = (m * G) ** 1.5 / math.sqrt(2 * RHO0 * A)
            etas.append(p_ideal / (p_elec - p_avionics_w))
        eta = math.exp(sum(math.log(e) for e in etas) / len(etas))     # geometric mean
        return cls(A, eta, eta_drive, p_avionics_w, cda_m2, usable_wh=usable)

    def _p_ideal(self, thrust_n: float) -> float:
        return thrust_n ** 1.5 / math.sqrt(2 * self.rho * self.rotor_area_m2)

    def hover_w(self, mass_kg: float) -> float:
        return self._p_ideal(mass_kg * G) / self.eta + self.p_avionics_w

    def forward_w(self, mass_kg: float, v_ms: float, headwind_ms: float = 0.0,
                  climb_ms: float = 0.0) -> float:
        """Electrical power at ground speed ``v_ms`` into a headwind (negative = tailwind)."""
        w = mass_kg * G
        v_air = max(0.0, v_ms + headwind_ms)
        drag = 0.5 * self.rho * self.cda_m2 * v_air * v_air
        thrust = math.hypot(w, drag)
        vh = math.sqrt(thrust / (2 * self.rho * self.rotor_area_m2))
        vi = math.sqrt(max(1e-9, (-v_air ** 2 + math.sqrt(v_air ** 4 + 4 * vh ** 4)) / 2))
        p_ind = thrust * (vi + max(0.0, climb_ms))
        # profile power from the hover split implied by the figure of merit
        p_ideal_h = self._p_ideal(thrust)
        p0 = p_ideal_h * (1.0 / self.figure_of_merit - 1.0)
        mu = v_air / self.tip_speed_ms
        p_prof = p0 * (1 + 3 * mu * mu)
        p_par = drag * v_air
        return (p_ind + p_prof + p_par) / self.eta_drive + self.p_avionics_w

    def endurance_min(self, mass_kg: float) -> float:
        return self.usable_wh / self.hover_w(mass_kg) * 60.0

    def with_air(self, temp_c: float, elevation_m: float) -> "PowerModel":
        from dataclasses import replace
        return replace(self, rho=air_density(temp_c, elevation_m))
