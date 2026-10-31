"""GNSS horizontal position error as a first-order Gauss-Markov process.

The K3-A Pro's standard dual GNSS is specified at about +-2 m horizontal hover
accuracy (vendor listings; the older K3-A manual says 1.5 m); with RTK it is
centimetre-level. With a 4 m swath that error is not noise: a slowly wandering
cross-track offset opens skip strips and double-dosed strips between passes,
which the mission simulator turns into under-dosed cells.
"""
from __future__ import annotations

import math

import numpy as np


class GnssError:
    def __init__(self, rtk: bool = False, sigma_h_m: float | None = None, tau_s: float | None = None,
                 seed: int = 0):
        # 95 % horizontal radius ~ 2.45 sigma for a 2-D Gaussian: +-2 m -> sigma ~ 0.8 m
        self.sigma = sigma_h_m if sigma_h_m is not None else (0.015 if rtk else 0.8)
        self.tau = tau_s if tau_s is not None else (10.0 if rtk else 60.0)
        self.rng = np.random.default_rng(seed)
        self.e = self.rng.normal(0.0, self.sigma, 2)

    def step(self, dt_s: float) -> tuple[float, float]:
        a = math.exp(-dt_s / self.tau)
        self.e = a * self.e + self.sigma * math.sqrt(1 - a * a) * self.rng.standard_normal(2)
        return float(self.e[0]), float(self.e[1])

    def series(self, n: int, dt_s: float) -> np.ndarray:
        return np.array([self.step(dt_s) for _ in range(n)])
