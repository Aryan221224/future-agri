"""Spray system: JIYI "linkage" flow, lateral deposition pattern, effective
swath (CV <= 25 %) and drift.

Tier-1 model for optimisation. The lateral pattern is four nozzle plumes (one
under each rotor) whose spread grows with height; a cross-wind shifts the
deposit downwind and loses a drift fraction off target. Coefficients are
labelled assumptions chosen so the 2.5 m calm effective swath falls in the
measured 4.5-7.9 m range of comparable 10 L-class drones (research brief
section 1.2). **Calibrate with an ISO 24253-style collector test of this
drone's nozzles at 2/3/4 m and 3/5/7 m/s before trusting a uniformity number.**
"""
from __future__ import annotations

import math

import numpy as np
from scipy.special import erf

from .spec import DroneSpec


class SprayModel:
    def __init__(self, spec: DroneSpec, sigma0: float = 0.35, sigma_per_m: float = 0.45,
                 shift_s: float = 0.30, drift_k: float = 0.012, wind_spread: float = 0.10,
                 turbulence: float = 0.35):
        self.spec = spec
        self.offsets = np.array(spec.nozzle_offsets_m)
        self.sigma0, self.sigma_per_m = sigma0, sigma_per_m
        self.shift_s = shift_s            # deposit displacement per (m/s cross-wind x m height)
        self.drift_k = drift_k            # off-target fraction per (m/s x m)
        self.wind_spread = wind_spread    # extra spread per m/s wind
        # pass-to-pass cross-wind fluctuation as a fraction of the mean wind
        # (turbulence intensity; gusts shift each pass differently -> striping)
        self.turbulence = turbulence

    # -- flow -------------------------------------------------------------
    def flow_lpm(self, v_ms: float, rate_l_ha: float, swath_m: float, pump_lpm: float) -> float:
        """Linkage mode: flow proportional to ground speed, pump-capped, off below cut-off."""
        if v_ms < self.spec.pump_cutoff_ms:
            return 0.0
        q = rate_l_ha * swath_m * v_ms * 60.0 / 10_000.0
        return min(q, pump_lpm)

    def achieved_rate(self, v_ms: float, rate_l_ha: float, swath_m: float, pump_lpm: float) -> float:
        q = self.flow_lpm(v_ms, rate_l_ha, swath_m, pump_lpm)
        if q <= 0:
            return 0.0
        return q * 10_000.0 / (swath_m * v_ms * 60.0)

    # -- pattern ----------------------------------------------------------
    def sigma(self, height_m: float, wind_ms: float = 0.0) -> float:
        return self.sigma0 + self.sigma_per_m * height_m + self.wind_spread * abs(wind_ms)

    def shift(self, height_m: float, wind_cross_ms: float) -> float:
        return self.shift_s * wind_cross_ms * height_m

    def off_target_frac(self, height_m: float, wind_ms: float, v_ms: float = 5.0) -> float:
        """Fraction of the sprayed volume carried beyond the swath as fine drift."""
        return float(min(0.4, self.drift_k * abs(wind_ms) * height_m * (1.0 + 0.05 * v_ms)))

    def lateral_density(self, y: np.ndarray, height_m: float, wind_cross_ms: float = 0.0,
                        cell_w: float = 0.0) -> np.ndarray:
        """Deposit density (1/m) at lateral offset ``y`` from the flight line; integrates
        to (1 - off_target). With ``cell_w`` > 0 it is averaged over a cell width."""
        s = self.sigma(height_m, wind_cross_ms)
        mu = self.offsets + self.shift(height_m, wind_cross_ms)
        keep = 1.0 - self.off_target_frac(height_m, wind_cross_ms)
        y = np.asarray(y, dtype=float)[..., None]
        if cell_w > 0:
            a = (y - cell_w / 2 - mu) / (s * math.sqrt(2))
            b = (y + cell_w / 2 - mu) / (s * math.sqrt(2))
            dens = 0.5 * (erf(b) - erf(a)) / cell_w
        else:
            dens = np.exp(-0.5 * ((y - mu) / s) ** 2) / (s * math.sqrt(2 * math.pi))
        return keep * dens.mean(axis=-1)

    def pass_wind(self, wind_cross_ms: float, rng: np.random.Generator) -> float:
        """Cross-wind seen by one pass: the mean plus a turbulent fluctuation."""
        return wind_cross_ms + self.turbulence * abs(wind_cross_ms) * rng.standard_normal()

    def pattern_cv(self, height_m: float, spacing_m: float, wind_cross_ms: float = 0.0,
                   n_draws: int = 8, seed: int = 0) -> float:
        """Expected CV of the summed deposit across passes at ``spacing_m``. A steady
        cross-wind shifts every pass alike (uniformity unchanged); its turbulent part
        shifts each pass differently, which is what stripes a real field."""
        rng = np.random.default_rng(seed)
        y = np.linspace(-6 * spacing_m, 6 * spacing_m, 768, endpoint=False)
        core = np.abs(y) < 2 * spacing_m
        cvs = []
        for _ in range(n_draws if wind_cross_ms else 1):
            tot = np.zeros_like(y)
            for k in range(-20, 21):
                w = self.pass_wind(wind_cross_ms, rng)
                tot += self.lateral_density(y - k * spacing_m, height_m, w)
            c = tot[core]
            cvs.append(c.std() / c.mean())
        return float(np.mean(cvs))

    def effective_swath(self, height_m: float, wind_cross_ms: float = 0.0,
                        cv_max: float = 0.25) -> float:
        """Widest pass spacing whose deposit CV is <= ``cv_max`` (ASABE/UAPASTF convention)."""
        best = 0.0
        for w in np.arange(0.5, 12.01, 0.1):
            if self.pattern_cv(height_m, float(w), wind_cross_ms) <= cv_max:
                best = float(w)
            else:
                break
        return round(best, 1)
