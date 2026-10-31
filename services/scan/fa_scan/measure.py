"""Measurement model of one camera-drone scan, at the scan's GSD.

* Canopy cover per cell: truth + error (sd grows with GSD; assumption 0.03 +
  0.5 x GSD[m], i.e. 0.035 at 1 cm).
* Growth stage: zone truth + error (sd 0.05 DVS; assumption).
* Weeds: per-cell counts detected with the **measured** RF-DETR recall-vs-
  object-size curve (``detector_wg_medium.json``) evaluated at object size in
  pixels at this GSD, plus measured false positives per m2; the product reports
  a recall-corrected density (the pipeline knows its own calibration).
  Weed size grows with days after sowing (assumption 1 cm + 0.15 cm/day, capped).
* Late blight: a cell is flagged with probability 1 - exp(-S / s_det), s_det =
  0.5 % severity at 1 cm GSD, proportional to GSD (assumption).

Everything assumed is named here and in the report until the H200 GSD sweep
and field labels replace it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

DATA = Path(__file__).parent / "data"


@dataclass
class DetectorModel:
    gsd_ref_m: float
    bins_px: list
    recall_right: list
    weed_fp_per_m2: float
    weed_precision: float
    provenance: str

    @classmethod
    @lru_cache(maxsize=None)
    def load(cls, name: str = "detector_wg_medium.json") -> "DetectorModel":
        d = json.loads((DATA / name).read_text())
        rec = d["weed"]["recall_right_group"]
        return cls(d["gsd_ref_m"], d["size_bins_px"], rec, d["weed_fp_per_m2"],
                   d["weed_precision"], d["provenance"])

    def _curve(self):
        b = self.bins_px
        mids = [0.5 * (lo + min(hi, 2 * lo + 32)) for lo, hi in zip(b[:-1], b[1:])]
        pts = [(m, r) for m, r in zip(mids, self.recall_right) if r is not None]
        # the measured last bin (>96 px, n=12) is noisy: hold the plateau instead
        plateau = float(np.median([r for _, r in pts[-4:-1]])) if len(pts) > 4 else pts[-1][1]
        xs = [0.0] + [p[0] for p in pts[:-1]] + [200.0]
        ys = [0.0] + [p[1] for p in pts[:-1]] + [plateau]
        return np.array(xs), np.array(ys)

    def weed_recall(self, size_cm: float, gsd_m: float) -> float:
        px = (size_cm / 100.0) / gsd_m
        xs, ys = self._curve()
        return float(np.interp(px, xs, ys))


@dataclass
class ScanObservation:
    day_index: int
    gsd_m: float
    cc_cells: np.ndarray
    cc_sd: float
    dvs_cells: np.ndarray
    dvs_sd: float
    weed_detections: np.ndarray        # raw detections per cell (incl. false positives)
    weed_density_est: np.ndarray       # recall- and FP-corrected plants/m2
    weed_density_sd: np.ndarray
    weed_recall_used: float
    blight_flag: np.ndarray            # cells where lesions were seen
    blight_sev_est: float              # mean observed severity over flagged cells (0 if none)
    assumptions: list = field(default_factory=list)


def simulate_scan(state, grid, plan, rng: np.random.Generator, crop: str,
                  days_after_sowing: int, detector: DetectorModel | None = None) -> ScanObservation:
    det = detector or DetectorModel.load()
    g = plan.gsd_m
    n = grid.n_cells
    cell_m2 = grid.cell_m ** 2
    cc_sd = 0.03 + 0.5 * g
    cc = np.clip(state.cc + rng.normal(0, cc_sd, n), 0.0, 1.0)
    dvs_sd = 0.05
    dvs = np.clip(state.dvs + rng.normal(0, dvs_sd, n), 0.0, 2.5)
    size_cm = float(np.clip(1.0 + 0.15 * max(0, days_after_sowing), 1.0, 12.0))
    rec = det.weed_recall(size_cm, g)
    true_counts = rng.poisson(np.maximum(state.weeds_m2, 0) * cell_m2)
    detected = rng.binomial(true_counts, rec)
    fp = rng.poisson(det.weed_fp_per_m2 * cell_m2, n)
    raw = detected + fp
    if rec > 0.05:
        est = np.maximum(0.0, (raw - det.weed_fp_per_m2 * cell_m2) / rec) / cell_m2
        sd = np.sqrt(np.maximum(raw, 1)) / rec / cell_m2
    else:
        est = np.zeros(n)
        sd = np.full(n, np.inf)
    s_det = 0.005 * (g / 0.01)
    p_flag = 1.0 - np.exp(-np.maximum(state.blight_sev, 0) / max(s_det, 1e-6))
    flag = rng.random(n) < p_flag
    sev_est = float(np.clip(state.blight_sev[flag].mean() * np.exp(rng.normal(0, 0.3)), 0, 1)) \
        if flag.any() else 0.0
    return ScanObservation(days_after_sowing, g, cc, cc_sd, dvs, dvs_sd, raw, est, sd, rec, flag,
                           sev_est, [
                               f"cover error sd {cc_sd:.3f} (assumed 0.03 + 0.5*GSD)",
                               f"weed size {size_cm:.1f} cm (assumed growth), recall {rec:.2f} from the "
                               "measured RF-DETR size curve",
                               f"blight detection scale {s_det:.4f} severity (assumed)"])
