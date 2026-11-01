"""Import a real camera-drone scan into the twin's observation type.

The scan pipeline (orthomosaic + detector, e.g. RF-DETR on the GPU server)
delivers detections as GeoJSON points in WGS84 with a ``class`` of ``weed`` or
``disease`` (``blight`` / ``lesion`` accepted), optional ``confidence`` and, for
disease, ``severity`` (0-1). Weed counts per cell are corrected with the same
measured detector recall-vs-size curve and false-positive rate the simulator
uses, so a real scan and a simulated one mean the same thing to the twin.
Canopy cover per cell comes from the orthomosaic or Sentinel-2 when supplied;
otherwise it is marked uninformative (huge error) so the twin ignores it.
"""
from __future__ import annotations

import numpy as np

from .measure import DetectorModel, ScanObservation

WEED = {"weed", "weeds"}
DISEASE = {"disease", "blight", "lesion", "late_blight"}


def cell_index_map(grid) -> np.ndarray:
    """(ny, nx) -> active-cell index, -1 outside the field."""
    m = np.full(grid.shape, -1, dtype=int)
    m[grid.mask] = np.arange(grid.n_cells)
    return m


def cells_of_lonlat(grid, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    x, y = tr.transform(np.asarray(lon, float), np.asarray(lat, float))
    c = grid.cell_m
    col = np.floor((np.asarray(x) - (grid.xs[0] - c / 2)) / c).astype(int)
    row = np.floor((np.asarray(y) - (grid.ys[0] - c / 2)) / c).astype(int)
    ny, nx = grid.shape
    inside = (col >= 0) & (col < nx) & (row >= 0) & (row < ny)
    out = np.full(len(col), -1, dtype=int)
    idx = cell_index_map(grid)
    out[inside] = idx[row[inside], col[inside]]
    return out


def import_scan(geojson: dict, grid, gsd_m: float, day_index: int,
                cover_cells: np.ndarray | None = None, dvs: float | None = None,
                detector: DetectorModel | None = None, fp_per_m2: float | None = None
                ) -> tuple[ScanObservation, dict]:
    if not isinstance(geojson, dict):
        raise ValueError("scan must be a GeoJSON object")
    if geojson.get("type") == "FeatureCollection":
        feats = geojson.get("features") or []
    elif geojson.get("type") == "Feature":
        feats = [geojson]
    else:
        raise ValueError("scan must be a GeoJSON FeatureCollection of points")
    det = detector or DetectorModel.load()
    fp = det.weed_fp_per_m2 if fp_per_m2 is None else float(fp_per_m2)
    n, cell_m2 = grid.n_cells, grid.cell_m ** 2
    rep = {"points": 0, "weeds": 0, "disease": 0, "outside": 0, "bad": 0, "errors": []}
    pts, kinds, sev = [], [], []
    for i, f in enumerate(feats):
        g = (f or {}).get("geometry") or {}
        props = (f or {}).get("properties") or {}
        cls = str(props.get("class", "")).strip().lower()
        coords = g.get("coordinates")
        if g.get("type") != "Point" or not isinstance(coords, (list, tuple)) or len(coords) < 2:
            rep["bad"] += 1
            rep["errors"].append(f"feature {i}: geometry {g.get('type')!s} is not a Point")
            continue
        if cls not in WEED | DISEASE:
            rep["bad"] += 1
            rep["errors"].append(f"feature {i}: class '{cls}' is not weed or disease")
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            rep["bad"] += 1
            rep["errors"].append(f"feature {i}: coordinates are not numbers")
            continue
        pts.append((lon, lat))
        kinds.append("weed" if cls in WEED else "disease")
        sev.append(float(np.clip(float(props.get("severity", 0.05) or 0.05), 0.0, 1.0)))
    cell = cells_of_lonlat(grid, [p[0] for p in pts], [p[1] for p in pts]) if pts else np.array([], int)
    raw = np.zeros(n)
    flag = np.zeros(n, bool)
    sevs = []
    for c, k, s in zip(cell, kinds, sev):
        if c < 0:
            rep["outside"] += 1
            continue
        rep["points"] += 1
        if k == "weed":
            rep["weeds"] += 1
            raw[c] += 1
        else:
            rep["disease"] += 1
            flag[c] = True
            sevs.append(s)
    size_cm = float(np.clip(1.0 + 0.15 * max(0, day_index), 1.0, 12.0))
    rec = det.weed_recall(size_cm, gsd_m)
    if rec > 0.05:
        est = np.maximum(0.0, (raw - fp * cell_m2) / rec) / cell_m2
        sd = np.sqrt(np.maximum(raw, 1)) / rec / cell_m2
    else:
        est, sd = np.zeros(n), np.full(n, np.inf)
    assumptions = [f"weed size {size_cm:.1f} cm (assumed growth), recall {rec:.2f} from the measured "
                   f"RF-DETR size curve; false positives {fp:.2f}/m2 removed"]
    if cover_cells is not None and len(cover_cells) == n:
        cc, cc_sd = np.clip(np.asarray(cover_cells, float), 0, 1), 0.05
    else:
        cc, cc_sd = np.full(n, 0.5), 10.0
        assumptions.append("canopy cover not supplied with the scan: marked uninformative")
    dvs_v, dvs_sd = (float(dvs), 0.1) if dvs is not None else (1.0, 10.0)
    if dvs is None:
        assumptions.append("growth stage not supplied: marked uninformative")
    obs = ScanObservation(day_index, gsd_m, cc, cc_sd, np.full(n, dvs_v), dvs_sd, raw, est, sd, rec,
                          flag, float(np.mean(sevs)) if sevs else 0.0, assumptions)
    return obs, rep
