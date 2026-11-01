"""Weed detection on a drone orthomosaic (GeoTIFF) with the trained RF-DETR.

The orthomosaic is read in overlapping tiles (windowed, so a multi-gigabyte
mosaic never sits in memory), each tile goes through the same detector the
aircraft uses (:mod:`fa_gateway.rfdetr`), and every weed detection becomes a
GeoJSON point at its box centre in WGS84. Overlapping tiles see the same plant
twice; detections closer than ``merge_m`` are merged, keeping the higher score.
The result is what :func:`fa_scan.importer.import_scan` ingests.

The detector is found through ``FA_DETECTOR_WEIGHTS``: an ``ml/runs/<name>``
directory with ``run.json`` (classes, resolution) and a TensorRT engine, an ONNX
model or the PyTorch checkpoint. Classes named in ``FA_DETECTOR_CROP`` (default:
the crops of the WeedsGalore and template taxonomies) are the crop, not weeds.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np

CROP_DEFAULT = "maize,crop,potato,wheat,rice,soybean,soy,cotton,sugarbeet,beet"
TILE, OVERLAP = 640, 96
MERGE_M = 0.05


def crop_classes() -> set[str]:
    return {c.strip().lower() for c in os.environ.get("FA_DETECTOR_CROP", CROP_DEFAULT).split(",") if c.strip()}


def weights_dir() -> Path | None:
    w = os.environ.get("FA_DETECTOR_WEIGHTS", "").strip()
    if not w:
        return None
    p = Path(w).expanduser()
    return p if (p / "run.json").is_file() else None


def backend() -> str:
    """PyTorch FP32 on the GPU server unless overridden: it is the reference and the fastest there
    (9.7 ms per 608 px tile on the H200). ONNX and FP16 match it (H200, 1 Nov 2026: detections
    matched one-to-one by IoU, recall 0.995, precision 0.996-0.997, class agreement 0.992-0.995;
    ml/results/wg-medium/latency.json), so the ONNX export is fit to build a TensorRT engine on
    the aircraft's own computer."""
    return os.environ.get("FA_DETECTOR_BACKEND", "torch")


def load_detector():
    run = weights_dir()
    if run is None:
        raise RuntimeError("weed detection needs the trained detector: connect the GPU server (System), "
                           "or set FA_DETECTOR_WEIGHTS to an ml/runs/<name> folder on this computer")
    from fa_gateway.rfdetr import detector_from_run
    return detector_from_run(run, backend=backend())


def _tiles(n: int, tile: int, step: int) -> list[int]:
    if n <= tile:
        return [0]
    out = list(range(0, n - tile, step))
    out.append(n - tile)
    return sorted(set(out))


def _gsd_m(ds) -> float:
    res = abs(ds.transform.a)
    if ds.crs is not None and ds.crs.is_geographic:
        lat = (ds.bounds.top + ds.bounds.bottom) / 2
        return res * 111_320.0 * math.cos(math.radians(lat))
    return res * (ds.crs.linear_units_factor[1] if ds.crs is not None and ds.crs.is_projected else 1.0)


def _rgb(ds, window) -> np.ndarray | None:
    """HxWx3 uint8 for a window, or None when the window is empty (no data, black, transparent)."""
    bands = ds.read(indexes=[1, 2, 3] if ds.count >= 3 else [1, 1, 1], window=window)
    if ds.count >= 4:                                     # alpha
        alpha = ds.read(4, window=window)
        if not alpha.any():
            return None
    if bands.dtype != np.uint8:                           # 16-bit or float mosaics: stretch to 8-bit
        b = bands.astype(np.float32)
        lo, hi = np.percentile(b, (1, 99)) if b.size else (0.0, 1.0)
        bands = np.clip((b - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
    if not bands.any():
        return None
    return np.ascontiguousarray(np.transpose(bands, (1, 2, 0)))


def detect_orthomosaic(path, detector, tile: int = TILE, overlap: int = OVERLAP, merge_m: float = MERGE_M,
                       progress=None, check=None) -> tuple[dict, dict]:
    """Detect weeds over a GeoTIFF; returns (GeoJSON FeatureCollection, summary)."""
    import rasterio
    from rasterio.warp import transform as warp
    from rasterio.windows import Window

    names = [str(c).lower() for c in getattr(detector, "object_classes", getattr(detector, "classes", []))]
    crop = crop_classes()
    step = max(1, tile - overlap)
    pts: list[tuple[float, float, float, str]] = []      # x, y (dataset CRS), score, label
    n_crop = skipped = 0
    with rasterio.open(path) as ds:
        if ds.crs is None:
            raise ValueError("the orthomosaic has no coordinate system: export it as a GeoTIFF")
        gsd = _gsd_m(ds)
        xs, ys = _tiles(ds.width, tile, step), _tiles(ds.height, tile, step)
        total = len(xs) * len(ys)
        k = 0
        for row in ys:
            for col in xs:
                k += 1
                w, h = min(tile, ds.width - col), min(tile, ds.height - row)
                img = _rgb(ds, Window(col, row, w, h))
                if img is None:
                    skipped += 1
                else:
                    for d in detector.infer(img):
                        cls = int(np.argmax(d.probs))
                        if cls >= len(names):                 # background
                            continue
                        label = names[cls]
                        if label in crop:
                            n_crop += 1
                            continue
                        cx, cy = (d.bbox[0] + d.bbox[2]) / 2 + col, (d.bbox[1] + d.bbox[3]) / 2 + row
                        x, y = ds.transform * (cx, cy)
                        pts.append((x, y, float(d.score), label))
                if progress and (k % 8 == 0 or k == total):
                    progress(k / total, f"tile {k}/{total}: {len(pts)} weed detections")
                if check:
                    check()
        crs = ds.crs
    merged = _merge(pts, merge_m, crs)
    if merged:
        lon, lat = warp(crs, "EPSG:4326", [p[0] for p in merged], [p[1] for p in merged])
    else:
        lon, lat = [], []
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lo, 8), round(la, 8)]},
              "properties": {"class": "weed", "label": p[3], "score": round(p[2], 4)}}
             for p, lo, la in zip(merged, lon, lat)]
    info = {"weeds": len(feats), "crop": n_crop, "tiles": total - skipped,
            "tiles_total": total, "skipped": skipped, "gsd_m": round(gsd, 5),
            "model": getattr(detector, "model_version", type(detector).__name__)}
    return {"type": "FeatureCollection", "features": feats}, info


def _merge(pts, merge_m: float, crs) -> list[tuple[float, float, float, str]]:
    """Greedy merge of detections closer than ``merge_m`` metres (highest score wins)."""
    if not pts:
        return []
    unit = 1.0
    if crs is not None and crs.is_geographic:
        unit = 1.0 / 111_320.0
    r = merge_m * unit
    order = sorted(pts, key=lambda p: -p[2])
    kept: list[tuple[float, float, float, str]] = []
    cell: dict[tuple[int, int], list[int]] = {}
    for p in order:
        gx, gy = int(math.floor(p[0] / r)), int(math.floor(p[1] / r))
        near = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in cell.get((gx + dx, gy + dy), ()):
                    q = kept[i]
                    if (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 <= r * r:
                        near = True
                        break
                if near:
                    break
            if near:
                break
        if not near:
            cell.setdefault((gx, gy), []).append(len(kept))
            kept.append(p)
    return kept


def detect_job_factory(state, ortho_path):
    """The ``detect`` job. ``ortho_path(job)`` gives the local GeoTIFF (the console's
    upload, or the worker's downloaded copy)."""

    def job(job: dict, ctx) -> dict:
        ctx.progress(0.01, "loading the weed detector")
        det = load_detector()
        path = ortho_path(job)
        ctx.progress(0.03, "reading the orthomosaic")
        gj, info = detect_orthomosaic(path, det, progress=lambda f, m: ctx.progress(0.03 + 0.95 * f, m),
                                      check=ctx.check)
        return {"summary": {"weeds": info["weeds"], "tiles": info["tiles"], "gsd_m": info["gsd_m"]},
                "geojson": gj, "gsd_m": info["gsd_m"], "info": info}

    return job


TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def console_ortho_path(store, job: dict) -> Path:
    """Where the console keeps a job's uploaded orthomosaic (``FA_HOME/farms/<id>/ortho``)."""
    name = str(job["params"]["ortho"])
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("bad orthomosaic name")
    return store.farm_dir(job["farm_id"]) / "ortho" / name


def ingest_detections(sessions, job: dict, result: dict) -> str:
    """Feed a finished detection into the farm's mission as a scan."""
    gj = (result or {}).get("geojson")
    if not gj or not gj.get("features"):
        return "finished: no weeds detected, nothing imported"
    gsd = min(0.1, max(0.001, float(result.get("gsd_m") or 0.01)))
    rep = sessions.upload_scan(job["farm_id"], gj, gsd)["report"]
    return (f"{rep['weeds']} weed detections imported into the mission"
            + (f" ({rep['outside']} outside the field)" if rep.get("outside") else ""))
