"""The camera on the spraying drone, simulated.

Along the flown track it reports, per cell, what a real on-board agent would: raw weed
detections, the area seen, the GSD and lesions seen. Counts use the same measured
detector model (recall by plant size and GSD, false positives by GSD) as a survey scan,
so a simulated run and a real one mean the same thing to the twin. Cells the camera did
not cover are NaN (unknown), never zero.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .measure import DetectorModel, ScanObservation, _weed_counts


@dataclass
class SprayerCamera:
    # assumed: a typical sprayer FPV/survey camera; replace with the calibration file
    hfov_deg: float = 70.0
    width_px: int = 1920
    lookahead_m: float = 2.0
    name: str = "generic 1080p, 70° HFOV"

    def footprint_m(self, height_m: float) -> float:
        return 2.0 * float(height_m) * math.tan(math.radians(self.hfov_deg) / 2.0)

    def gsd_m(self, height_m: float) -> float:
        return self.footprint_m(height_m) / self.width_px


def cells_along(track: list[dict], grid, width_m: float, sortie: int | None = None,
                spraying_only: bool = True) -> np.ndarray:
    """Cells whose centre lies within ``width_m``/2 of the track (grid-local metres), for one
    sortie and/or only where the boom was spraying. Track keyframes sit at segment ends
    (fa_drone.mission), so a segment's sortie and spray state are those of its end point."""
    from shapely import contains_xy
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union
    pts = [p for p in track if sortie is None or p.get("sortie") == sortie]
    segs = []
    for a, b in zip(pts, pts[1:]):
        if spraying_only and not b.get("spraying"):
            continue
        pa, pb = (float(a["x"]), float(a["y"])), (float(b["x"]), float(b["y"]))
        segs.append(LineString([pa, pb]) if pa != pb else Point(pa))
    if not segs:
        return np.zeros(grid.n_cells, bool)
    area = unary_union([s.buffer(width_m / 2.0, cap_style="flat" if s.geom_type == "LineString" else "round")
                        for s in segs])
    xy = grid.cells_xy
    return np.asarray(contains_xy(area, xy[:, 0], xy[:, 1]), bool)


def observe(state, grid, seen: np.ndarray, gsd_m: float, das: int, rng: np.random.Generator,
            detector: DetectorModel | None = None) -> ScanObservation:
    det = detector or DetectorModel.load()
    seen = np.asarray(seen, bool)
    n, cell_m2 = grid.n_cells, grid.cell_m ** 2
    size_cm = float(np.clip(1.0 + 0.15 * max(0, das), 1.0, 12.0))      # as simulate_scan (assumed growth)
    rec = det.weed_recall(size_cm, gsd_m)
    raw, est, sd = _weed_counts(state.weeds_m2, cell_m2, rec, det.fp_per_m2(gsd_m), rng)
    raw = np.where(seen, raw, 0)
    est = np.where(seen, est, np.nan)
    sd = np.where(seen, sd, np.inf)
    cc_sd = 0.03 + 0.5 * gsd_m                                          # as simulate_scan (assumed)
    cc = np.where(seen, np.clip(state.cc + rng.normal(0, cc_sd, n), 0.0, 1.0), 0.5)
    s_det = 0.005 * (gsd_m / 0.01)                                     # as simulate_scan (assumed)
    flag = seen & (rng.random(n) < 1.0 - np.exp(-np.maximum(state.blight_sev, 0) / max(s_det, 1e-6)))
    sev = float(np.clip(state.blight_sev[flag].mean(), 0, 1)) if flag.any() else 0.0
    return ScanObservation(das, gsd_m, cc, cc_sd, np.full(n, 1.0), 10.0, raw, est, sd, rec, flag, sev,
                           [f"sprayer camera at GSD {gsd_m * 1000:.1f} mm: weed size {size_cm:.1f} cm "
                            f"(assumed growth), recall {rec:.2f} from the measured RF-DETR curve",
                            "growth stage not observed by the sprayer camera: marked uninformative"],
                           observed=seen)


def to_cell_messages(obs: ScanObservation, grid, t: str) -> list[dict]:
    """The per-cell items of a live message batch (schema fa.live/1, ``cells``) for the seen cells."""
    seen = np.ones(grid.n_cells, bool) if obs.observed is None else np.asarray(obs.observed, bool)
    a = float(grid.cell_m ** 2)
    return [{"t": t, "cell": int(i), "weeds": int(obs.weed_detections[i]), "area_m2": a,
             "gsd_m": float(obs.gsd_m), "blight": bool(obs.blight_flag[i])} for i in np.where(seen)[0]]
