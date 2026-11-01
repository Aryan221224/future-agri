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
    """Zones from the observed cover. With ``obs.observed`` set, only the covered cells form
    zones; the rest get zone -1 and NaN weeds (unknown, not zero)."""
    n = len(obs.cc_cells)
    seen = np.ones(n, bool) if getattr(obs, "observed", None) is None else np.asarray(obs.observed, bool)
    idx_seen = np.where(seen)[0]
    if len(idx_seen) == 0:
        raise ValueError("the scan observed no cell of this field")
    cc_seen = obs.cc_cells[idx_seen]
    k = max(1, min(k, len(idx_seen)))
    if k > 1:
        edges = np.quantile(cc_seen, np.linspace(0, 1, k + 1)[1:-1])
        z_seen = np.searchsorted(np.unique(edges), cc_seen, side="right")
    else:
        z_seen = np.zeros(len(idx_seen), int)
    ids = sorted(set(z_seen.tolist()))
    remap = {z: i for i, z in enumerate(ids)}
    zone = np.full(n, -1, int)
    zone[idx_seen] = [remap[z] for z in z_seen]
    cell_ha = grid.cell_m ** 2 / 1e4
    zones = []
    for i in range(len(ids)):
        sel = np.where(zone == i)[0]
        m = len(sel)
        finite = np.isfinite(obs.weed_density_sd[sel])
        wsd = (float(np.sqrt(np.sum(obs.weed_density_sd[sel][finite] ** 2)) / max(finite.sum(), 1))
               if finite.any() else float("inf"))
        w_sel = obs.weed_density_est[sel]
        zones.append(ZoneObs(
            i, sel, m * cell_ha,
            float(obs.cc_cells[sel].mean()),
            float(np.hypot(obs.cc_cells[sel].std(), obs.cc_sd / np.sqrt(m))),
            float(obs.dvs_cells[sel].mean()), float(obs.dvs_sd / np.sqrt(m) + obs.dvs_cells[sel].std()),
            float(np.nanmean(w_sel)) if np.isfinite(w_sel).any() else float("nan"), wsd))
    weed_map = np.where(seen, obs.weed_density_est, np.nan)
    weed_sd = np.where(seen, obs.weed_density_sd, np.inf)
    blight = obs.blight_flag & seen
    return DigitalFarm(obs.day_index, obs.gsd_m, zone, zones, weed_map, weed_sd, bool(blight.any()),
                       obs.blight_sev_est, blight, grid.cell_m, list(obs.assumptions))
