"""Measure the trained detector at coarser ground sample distances (H200).

A camera flown higher sees the same plants in fewer pixels. To emulate GSD
k x 2.5 mm without new imagery, k x k test tiles are each down-sampled by k
(area interpolation) and stitched into one 600 x 600 canvas -- the model then
sees k^2 times the ground per image, exactly as a higher flight would -- and the
ground-truth boxes are scaled and offset to match. Statistics are computed with
the same matching as ml/detector_stats.py.

    python ml/h200/gsd_sweep.py --run ml/runs/wg-medium --data ~/data/weedsgalore_coco \
        --factors 1 2 4 8 --out ml/results/wg-medium/gsd_sweep.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "gateway"))
from detector_stats import GSD_REF_M, SIZE_BINS, load_gts, stats_from  # noqa: E402


def canvases(coco: dict, img_dir: Path, k: int, size: int = 600):
    from PIL import Image
    classes, gts, _ = load_gts(coco)
    ims = sorted(coco["images"], key=lambda x: x["id"])
    per = k * k
    for s in range(0, len(ims) - per + 1, per):
        canvas = Image.new("RGB", (size, size))
        boxes = []
        for j, im in enumerate(ims[s:s + per]):
            r, c = divmod(j, k)
            tile = Image.open(img_dir / im["file_name"]).convert("RGB")
            w, h = tile.size
            small = tile.resize((w // k, h // k), Image.BOX)
            ox, oy = c * (size // k), r * (size // k)
            canvas.paste(small, (ox, oy))
            for x1, y1, x2, y2, nm in gts.get(im["id"], []):
                boxes.append((x1 / k + ox, y1 / k + oy, x2 / k + ox, y2 / k + oy, nm))
        yield np.asarray(canvas), boxes, size * size


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--split", default="test")
    ap.add_argument("--factors", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    from fa_gateway.rfdetr import detector_from_run
    det = detector_from_run(a.run)
    metrics = json.loads((a.run / "eval" / "metrics.json").read_text())
    thr = float(metrics["operating_threshold"])
    coco = json.loads((a.data / a.split / "_annotations.coco.json").read_text())
    classes, _, _ = load_gts(coco)
    res = {"model": metrics["model_version"], "threshold": thr, "size_bins_px": SIZE_BINS,
           "gsd_ref_m": GSD_REF_M, "by_factor": {}}
    for k in a.factors:
        imgs = []
        for arr, boxes, px in canvases(coco, a.data / a.split, k):
            q = det.queries(arr)
            imgs.append({"boxes": q.boxes_xyxy, "class_scores": q.class_scores, "gts": boxes, "pixels": px})
        st = stats_from(imgs, classes, thr, gsd_m=GSD_REF_M * k)
        st["canvases"] = len(imgs)
        res["by_factor"][str(k)] = st
        print(f"k={k} gsd={GSD_REF_M * k * 1000:.1f} mm canvases={len(imgs)} weed recall="
              f"{st['weed']['overall_recall_right_group']:.3f} precision={st['weed_precision']:.3f}", flush=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1))
    print("GSD_SWEEP_DONE", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
