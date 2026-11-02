"""Measurement model of one camera-drone scan, at the scan's GSD.

* Canopy cover per cell: truth + error (sd grows with GSD; assumption 0.03 +
  0.5 x GSD[m], i.e. 0.035 at 1 cm).
* Growth stage: zone truth + error (sd 0.05 DVS; assumption).
* Weeds: per-cell counts detected with the **measured** RF-DETR recall, a
  function of object size in pixels *and* of GSD: the H200 GSD sweep
  (``detector_wg_medium_gsd.json``: test tiles down-sampled to 2.5/5/10/20 mm)
  gives one recall-vs-pixels curve per GSD, interpolated in log GSD -- coarser
  imagery loses texture, so a plant of the same pixel size is found less often.
  False positives per m2 are measured per GSD the same way. The product reports
  a recall-corrected density (the pipeline knows its own calibration).
  Weed size grows with days after sowing (assumption 1 cm + 0.15 cm/day, capped).
* Late blight: a cell is flagged with probability 1 - exp(-S / s_det), s_det =
  0.5 % severity at 1 cm GSD, proportional to GSD (assumption).

Everything assumed is named here and in the report until field labels replace
it. Without the sweep file the single reference curve (2.5 mm) is used.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

DATA = Path(__file__).parent / "data"


MIN_BIN_N = 10                         # size bins with fewer objects are too noisy to use


def _mids(bins) -> list[float]:
    return [0.5 * (lo + min(hi, 2 * lo + 32)) for lo, hi in zip(bins[:-1], bins[1:])]


def _sweep_curves(path: Path) -> tuple[tuple, tuple]:
    """Per-GSD (gsd_m, px, recall) curves and (gsd_m, fp_per_m2) from the H200 sweep."""
    if not path.exists():
        return (), ()
    sw = json.loads(path.read_text())
    bins, g0 = sw["size_bins_px"], float(sw["gsd_ref_m"])
    mids = _mids(bins)
    n_ref = sw["by_factor"]["1"]["weed"]["n"]
    curves, fps = [], []
    for k_str, st in sorted(sw["by_factor"].items(), key=lambda kv: int(kv[0])):
        k, w = int(k_str), st["weed"]
        # the first bin (< 12 px) at factor k holds the plants smaller than 12k px at the
        # reference GSD: place it at their mean size, not at a fixed 6 px
        inside = [(n, m) for n, m, hi in zip(n_ref, mids, bins[1:]) if hi <= bins[1] * k]
        x0 = (sum(n * m for n, m in inside) / max(sum(n for n, _ in inside), 1)) / k if inside else mids[0]
        xs, ys = [0.0], [0.0]
        for b, (r, n) in enumerate(zip(w["recall_right_group"], w["n"])):
            if r is None or n < MIN_BIN_N:
                continue
            x = x0 if b == 0 else mids[b]
            if x > xs[-1]:
                xs.append(x)
                ys.append(float(r))
        curves.append((g0 * k, np.array(xs), np.array(ys)))     # beyond the last point: held (np.interp)
        fps.append((g0 * k, float(st["weed_fp_per_m2"])))
    return tuple(curves), tuple(fps)


def _log_interp(g: float, pts: list[tuple[float, float]]) -> float:
    gs = np.log([p[0] for p in pts])
    return float(np.interp(np.log(g), gs, [p[1] for p in pts]))


@dataclass
class DetectorModel:
    gsd_ref_m: float
    bins_px: list
    recall_right: list
    weed_fp_per_m2: float                 # at the reference GSD
    weed_precision: float
    provenance: str
    gsd_curves: tuple = ()                # ((gsd_m, px, recall), ...) from the H200 sweep, ascending GSD
    fp_by_gsd: tuple = ()                 # ((gsd_m, fp_per_m2), ...)

    @classmethod
    @lru_cache(maxsize=None)
    def load(cls, name: str = "detector_wg_medium.json") -> "DetectorModel":
        d = json.loads((DATA / name).read_text())
        rec = d["weed"]["recall_right_group"]
        curves, fps = _sweep_curves(DATA / name.replace(".json", "_gsd.json"))
        return cls(d["gsd_ref_m"], d["size_bins_px"], rec, d["weed_fp_per_m2"],
                   d["weed_precision"], d["provenance"], curves, fps)

    def fp_per_m2(self, gsd_m: float) -> float:
        """False weed detections per m2 at this GSD (fewer at coarse GSD: fewer pixels to fool)."""
        if not self.fp_by_gsd:
            return self.weed_fp_per_m2
        return _log_interp(gsd_m, list(self.fp_by_gsd))

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
        if not self.gsd_curves:
            xs, ys = self._curve()
            return float(np.interp(px, xs, ys))
        at = [(g, float(np.interp(px, xs, ys))) for g, xs, ys in self.gsd_curves]
        return _log_interp(gsd_m, at)


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
    #: cells the camera covered (None: the whole field). Unobserved cells carry no information:
    #: their weed estimate is NaN and they are left out of zones, so the twin keeps its prior there.
    observed: np.ndarray | None = None
    #: False when cover and growth stage were not measured (e.g. the sprayer camera reports
    #: weed counts only): the twin then updates the weed map and nothing else.
    crop_observed: bool = True


def _weed_counts(weeds_m2: np.ndarray, area_m2, rec: float, fp_m2: float, rng: np.random.Generator):
    """Raw detections (true plants found at ``rec`` + false positives) over ``area_m2`` per cell,
    and the recall- and FP-corrected density with its sd. Below 5 % recall the counts say
    nothing: estimate 0 with infinite sd."""
    w = np.maximum(np.asarray(weeds_m2, float), 0)
    area = np.broadcast_to(np.asarray(area_m2, float), w.shape)
    detected = rng.binomial(rng.poisson(w * area), rec)
    raw = detected + rng.poisson(fp_m2 * area)
    if rec > 0.05:
        est = np.maximum(0.0, (raw - fp_m2 * area) / rec) / area
        sd = np.sqrt(np.maximum(raw, 1)) / rec / area
    else:
        est, sd = np.zeros(len(w)), np.full(len(w), np.inf)
    return raw, est, sd


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
    raw, est, sd = _weed_counts(state.weeds_m2, cell_m2, rec, det.fp_per_m2(g), rng)
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
