"""Within-field variation for the ground-truth farm.

Real fields are not uniform: water-holding varies with texture and landscape
position, emergence is patchy, weeds come in clusters and disease spreads from
foci. These generators put that structure into the truth so that a scan, a
spot-spray and a skipped strip all have something real to find or miss. Every
parameter is a labelled assumption, set per run from the archetype and seed.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from .grid import FarmGrid


def gaussian_field(shape: tuple[int, int], cell_m: float, corr_m: float,
                   rng: np.random.Generator) -> np.ndarray:
    """Stationary Gaussian random field (mean 0, sd 1) with a Gaussian
    correlation length ``corr_m``, by spectral filtering of white noise."""
    ny, nx = shape
    py, px = ny + 2 * max(4, int(corr_m / cell_m)), nx + 2 * max(4, int(corr_m / cell_m))
    noise = rng.standard_normal((py, px))
    ky = np.fft.fftfreq(py, d=cell_m)
    kx = np.fft.fftfreq(px, d=cell_m)
    K2 = ky[:, None] ** 2 + kx[None, :] ** 2
    filt = np.exp(-2.0 * (np.pi ** 2) * (corr_m ** 2) * K2 / 2.0)
    f = np.real(np.fft.ifft2(np.fft.fft2(noise) * filt))
    oy, ox = (py - ny) // 2, (px - nx) // 2
    f = f[oy:oy + ny, ox:ox + nx]
    sd = f.std()
    return (f - f.mean()) / (sd if sd > 0 else 1.0)


def soil_zones(grid: FarmGrid, base, twi_cells: np.ndarray | None, rng: np.random.Generator,
               k: int = 4, cv: float = 0.15, corr_m: float = 40.0):
    """Management/soil zones from a water-holding factor
    s = 1 + cv * (0.6 * GRF + 0.4 * standardised TWI), cut into ``k`` quantile
    classes. Each zone gets the base soil with its available water (SMFCF-SMW)
    scaled by the zone's mean factor. -> (zone_of_cell, [SoilParams]*k)."""
    f = grid.from_raster(gaussian_field(grid.shape, grid.cell_m, corr_m, rng))
    if twi_cells is not None and np.std(twi_cells) > 0:
        t = (twi_cells - np.mean(twi_cells)) / np.std(twi_cells)
        z = 0.6 * f + 0.4 * t
        z = (z - z.mean()) / (z.std() if z.std() > 0 else 1.0)
    else:
        z = f
    s = 1.0 + cv * z
    k = max(1, min(k, grid.n_cells))
    edges = np.quantile(s, np.linspace(0, 1, k + 1)[1:-1]) if k > 1 else np.array([])
    zone = np.searchsorted(edges, s, side="right")
    soils = []
    for i in range(k):
        sel = zone == i
        fac = float(s[sel].mean()) if sel.any() else 1.0
        awc = (base.SMFCF - base.SMW) * fac
        smfcf = min(base.SM0 - 0.02, base.SMW + awc)
        soils.append(replace(base, SMFCF=round(smfcf, 4)))
    return zone.astype(int), soils


def emergence_field(grid: FarmGrid, rng: np.random.Generator, cv: float = 0.08,
                    corr_m: float = 15.0) -> np.ndarray:
    """Multiplicative stand factor per cell, mean 1 (patchy emergence)."""
    f = grid.from_raster(gaussian_field(grid.shape, grid.cell_m, corr_m, rng))
    e = np.clip(1.0 + cv * f, 0.5, 1.5)
    return e / e.mean()


def weed_seedbank(grid: FarmGrid, rng: np.random.Generator, mean_m2: float,
                  clusters_per_ha: float = 3.0, radius_m: float = 12.0,
                  background_frac: float = 0.15) -> np.ndarray:
    """Weed density (plants/m2) per cell from a Neyman-Scott cluster process:
    Poisson parents, Gaussian clusters, plus a thin uniform background; scaled so
    the field mean equals ``mean_m2``."""
    xy = grid.cells_xy
    n_par = max(1, rng.poisson(clusters_per_ha * max(grid.area_ha, 0.01)))
    minx, miny, maxx, maxy = grid.bounds_local
    px = rng.uniform(minx, maxx, n_par)
    py = rng.uniform(miny, maxy, n_par)
    w = rng.gamma(2.0, 0.5, n_par)
    d = np.zeros(len(xy))
    for x0, y0, wi in zip(px, py, w):
        r2 = (xy[:, 0] - x0) ** 2 + (xy[:, 1] - y0) ** 2
        d += wi * np.exp(-r2 / (2 * radius_m ** 2))
    if d.sum() <= 0:
        d = np.ones(len(xy))
    d = d / d.mean() * (1.0 - background_frac) + background_frac
    d *= mean_m2 / d.mean()
    return d


def foci_onset(grid: FarmGrid, rng: np.random.Generator, n_foci: int,
               spread_m_per_day: float = 4.0) -> np.ndarray:
    """Days after field infection at which each cell becomes infected, by
    distance to the nearest of ``n_foci`` random foci (focal spread)."""
    xy = grid.cells_xy
    idx = rng.choice(len(xy), size=max(1, min(n_foci, len(xy))), replace=False)
    dist = np.min(np.hypot(xy[:, None, 0] - xy[None, idx, 0], xy[:, None, 1] - xy[None, idx, 1]),
                  axis=1)
    return np.floor(dist / spread_m_per_day).astype(int)
