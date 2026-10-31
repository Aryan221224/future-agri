"""The DigitalFarm: everything the twin knows about the field, built only from
scans. Management zones are derived from the *observed* cover (the twin cannot
know the truth's soil zones), each with a mean and an uncertainty.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class ZoneObs:
    zone_id: int
    cells: np.ndarray            # indices of grid cells in this zone
    area_ha: float
    cc: float
    cc_sd: float
    dvs: float
    dvs_sd: float
    weeds_m2: float
    weeds_sd: float


@dataclass
class DigitalFarm:
    day_index: int
    gsd_m: float
    zone_of_cell: np.ndarray
    zones: list[ZoneObs]
    weed_map: np.ndarray
    weed_map_sd: np.ndarray
    blight_seen: bool
    blight_sev_est: float
    blight_cells: np.ndarray
    cell_m: float
    assumptions: list = field(default_factory=list)

    @property
    def area_ha(self) -> float:
        return float(sum(z.area_ha for z in self.zones))


def build_digital_farm(obs, grid, k: int = 4) -> DigitalFarm:
    n = len(obs.cc_cells)
    k = max(1, min(k, n))
    if k > 1:
        edges = np.quantile(obs.cc_cells, np.linspace(0, 1, k + 1)[1:-1])
        zone = np.searchsorted(np.unique(edges), obs.cc_cells, side="right")
    else:
        zone = np.zeros(n, int)
    ids = sorted(set(zone.tolist()))
    remap = {z: i for i, z in enumerate(ids)}
    zone = np.array([remap[z] for z in zone])
    cell_ha = grid.cell_m ** 2 / 1e4
    zones = []
    for i in range(len(ids)):
        sel = np.where(zone == i)[0]
        m = len(sel)
        finite = np.isfinite(obs.weed_density_sd[sel])
        wsd = (float(np.sqrt(np.sum(obs.weed_density_sd[sel][finite] ** 2)) / max(finite.sum(), 1))
               if finite.any() else float("inf"))
        zones.append(ZoneObs(
            i, sel, m * cell_ha,
            float(obs.cc_cells[sel].mean()),
            float(np.hypot(obs.cc_cells[sel].std(), obs.cc_sd / np.sqrt(m))),
            float(obs.dvs_cells[sel].mean()), float(obs.dvs_sd / np.sqrt(m) + obs.dvs_cells[sel].std()),
            float(obs.weed_density_est[sel].mean()), wsd))
    return DigitalFarm(obs.day_index, obs.gsd_m, zone, zones, obs.weed_density_est.copy(),
                       obs.weed_density_sd.copy(), bool(obs.blight_flag.any()), obs.blight_sev_est,
                       obs.blight_flag.copy(), grid.cell_m, list(obs.assumptions))
