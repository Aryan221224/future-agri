#!/usr/bin/env python3
"""Measure the uplink reduction from edge inference.

The architecture claims that transferring derived products instead of imagery
reduces mission uplink volume by two to three orders of magnitude. That is a
measurable quantity, not a slogan, so this measures it on a realistic sortie:
frame count from the actual survey geometry, real JPEG compression, and the real
serialised size of the zone summaries the gateway would publish.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/gateway"):
    sys.path.insert(0, str(ROOT / p))

from fa_core.geo import CAMERAS, plan_survey  # noqa: E402
from fa_core.uncertainty import (StratifiedCalibrator, calibrate_classification,  # noqa: E402
                                 calibrate_regression)
from fa_gateway.perception import StubDetector  # noqa: E402
from fa_gateway.pipeline import EdgePipeline  # noqa: E402


def jpeg_bytes_per_frame(camera, quality: int = 88, sample_px: int = 512) -> float:
    """Estimate compressed bytes per frame, measured then scaled.

    Compressing a full 20 MP frame per sortie in a benchmark is wasteful, so we
    compress a representative tile of crop-like texture and scale by pixel count.
    Crop canopy is high-entropy -- it compresses far worse than the flat test
    images people usually benchmark with, which is why this uses textured noise
    rather than a gradient.
    """
    try:
        from PIL import Image
    except ImportError:
        return camera.image_width_px * camera.image_height_px * 3 * 0.12

    rng = np.random.default_rng(0)
    base = rng.integers(40, 150, (sample_px, sample_px, 3), dtype=np.uint8)
    yy, xx = np.mgrid[0:sample_px, 0:sample_px]
    rows = (np.sin(xx / 3.0) * 30).astype(np.int16)          # crop rows
    tex = (rng.normal(0, 18, (sample_px, sample_px, 1))).astype(np.int16)
    img = np.clip(base + rows[..., None] + tex, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="JPEG", quality=quality)
    per_px = buf.tell() / (sample_px * sample_px)
    return per_px * camera.image_width_px * camera.image_height_px


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--area-ha", type=float, default=25.0)
    ap.add_argument("--gsd-cm", type=float, default=3.0)
    ap.add_argument("--camera", default="mavic3m_rgb", choices=sorted(CAMERAS))
    ap.add_argument("--zones", type=int, default=8)
    ap.add_argument("--frames-per-zone-sample", type=int, default=6,
                    help="frames actually run through the detector per zone")
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / "bandwidth.json")
    a = ap.parse_args()

    cam = CAMERAS[a.camera]
    plan = plan_survey(cam, a.gsd_cm / 100.0)
    area_m2 = a.area_ha * 10_000
    n_frames = plan.frames_for_area(area_m2)

    raw_per_frame = cam.image_width_px * cam.image_height_px * 3
    jpg_per_frame = jpeg_bytes_per_frame(cam)
    raw_total = n_frames * raw_per_frame
    jpg_total = n_frames * jpg_per_frame

    # Build a calibrator so the summaries carry real conformal fields.
    rng = np.random.default_rng(0)
    classes = ["crop", "broadleaf_weed", "grass_weed", "disease_lesion",
               "nutrient_deficiency", "unknown"]
    n = 3000
    y = rng.integers(0, len(classes), n)
    lg = rng.normal(0, 1, (n, len(classes)))
    lg[np.arange(n), y] += rng.uniform(0.5, 3.0, n)
    e = np.exp(lg - lg.max(1, keepdims=True))
    probs = e / e.sum(1, keepdims=True)
    cls_cal = calibrate_classification(probs, y, classes, alpha=0.10, score="lac")
    reg_cal = calibrate_regression(rng.uniform(0, 6, n), rng.uniform(0, 6, n), alpha=0.10)
    calib = StratifiedCalibrator(alpha=0.10, global_cal=cls_cal, min_n=200)
    calib.strata[StratifiedCalibrator.key("rice", "tillering", "site1")] = cls_cal

    det = StubDetector(classes=classes)
    pipe = EdgePipeline(det, calib, classes, crop="rice", stage="tillering", site="site1")

    summaries, reviews_total, payload = [], 0, 0
    per_zone_frames = max(1, n_frames // a.zones)
    zone_area = a.area_ha / a.zones
    for z in range(a.zones):
        frames = [(rng.random((256, 256, 3)) * 255).astype(np.uint8)
                  for _ in range(a.frames_per_zone_sample)]
        s, rev = pipe.process_zone(f"zone-{z}", frames, zone_area,
                                   datetime.now(timezone.utc))
        # Scale the observed detection counts to the true frame count.
        scale = per_zone_frames / a.frames_per_zone_sample
        s.n_frames = per_zone_frames
        s.n_detections = int(s.n_detections * scale)
        s.class_counts = {k: int(v * scale) for k, v in s.class_counts.items()}
        s.density_per_ha = {k: round(v * scale, 2) for k, v in s.density_per_ha.items()}
        s.review_count = int(s.review_count * scale)
        summaries.append(s)
        reviews_total += s.review_count
        payload += s.payload_bytes()

    # Review items are fetched on demand, but budget a thumbnail for each.
    thumb_bytes = 24 * 1024
    review_payload = reviews_total * thumb_bytes

    res = {
        "scenario": {"area_ha": a.area_ha, "gsd_cm": a.gsd_cm, "camera": cam.name,
                     "altitude_m": round(plan.altitude_m, 1),
                     "front_overlap": plan.front_overlap, "side_overlap": plan.side_overlap,
                     "frames": n_frames, "zones": a.zones},
        "raw_uplink_bytes": int(raw_total),
        "jpeg_uplink_bytes": int(jpg_total),
        "derived_payload_bytes": int(payload),
        "review_thumbnail_bytes": int(review_payload),
        "derived_plus_review_bytes": int(payload + review_payload),
        "reduction_vs_raw": raw_total / max(payload, 1),
        "reduction_vs_jpeg": jpg_total / max(payload, 1),
        "reduction_vs_jpeg_incl_review": jpg_total / max(payload + review_payload, 1),
        "review_fraction": (sum(s.review_count for s in summaries) /
                            max(sum(s.n_detections for s in summaries), 1)),
        "mean_conformal_set_size": float(np.mean([s.mean_conformal_set_size for s in summaries])),
        "mean_inference_ms_stub": round(pipe.mean_latency_ms, 2),
        "example_summary": json.loads(summaries[0].to_json()),
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=2), encoding="utf-8")

    def mb(x): return x / 1e6
    print(f"sortie: {a.area_ha:.0f} ha at {a.gsd_cm:.0f} cm GSD, {cam.name}")
    print(f"  altitude {plan.altitude_m:.0f} m, {n_frames} frames "
          f"({plan.front_overlap:.0%}/{plan.side_overlap:.0%} overlap)\n")
    print(f"  raw imagery uplink        {mb(raw_total):10.1f} MB")
    print(f"  JPEG imagery uplink       {mb(jpg_total):10.1f} MB")
    print(f"  derived products only     {payload/1e3:10.1f} kB   "
          f"({res['reduction_vs_jpeg']:,.0f}x less than JPEG)")
    print(f"  + review thumbnails       {mb(payload+review_payload):10.3f} MB   "
          f"({res['reduction_vs_jpeg_incl_review']:,.1f}x less than JPEG)")
    print(f"\n  review fraction {res['review_fraction']:.1%}, "
          f"mean conformal set size {res['mean_conformal_set_size']:.2f}")
    print(f"\nwritten to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
