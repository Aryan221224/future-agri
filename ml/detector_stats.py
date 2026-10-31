"""Measured detection statistics of the real RF-DETR-Medium WeedsGalore model,
for the scan simulator: action-level (crop / weed) recall as a function of
object size in pixels, precision, false positives per square metre and crop/weed
confusion, at the operating threshold, from the cached H200 test predictions.

    python ml/detector_stats.py --run ~/h200/runs/wg-medium --data ~/h200/data \
        --out services/scan/fa_scan/data/detector_wg_medium.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

CROP = {"maize"}
SIZE_BINS = [0, 12, 16, 20, 24, 32, 48, 64, 96, 10_000]
GSD_REF_M = 0.0025          # WeedsGalore UAV imagery, ~2.5 mm/px


def iou_xyxy(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    aa = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    ab = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(aa[:, None] + ab[None, :] - inter, 1e-9)


def compute(run: Path, data: Path, split: str = "test", thr: float | None = None) -> dict:
    metrics = json.loads((run / "eval" / "metrics.json").read_text())
    thr = thr if thr is not None else float(metrics["operating_threshold"])
    coco = json.loads((data / split / "_annotations.coco.json").read_text())
    names = {c["id"]: c["name"] for c in coco["categories"]}
    classes = [names[k] for k in sorted(names)]
    z = np.load(run / "eval" / f"preds_{split}_torch.npz")
    gt_by_img: dict[int, list] = {}
    for a in coco["annotations"]:
        x, y, w, h = a["bbox"]
        gt_by_img.setdefault(a["image_id"], []).append((x, y, x + w, y + h, names[a["category_id"]]))
    img_px = {im["id"]: im["width"] * im["height"] for im in coco["images"]}

    det = {"weed": [], "crop": []}           # (size_px, detected_any, detected_as_right_group)
    tp_w = fp_w = 0
    fp_total = 0
    conf = {"weed_as_crop": 0, "weed_n": 0, "crop_as_weed": 0, "crop_n": 0}
    area_m2 = 0.0
    for k, iid in enumerate(z["image_ids"]):
        sc = z["class_scores"][k]
        s, lab = sc.max(axis=1), sc.argmax(axis=1)
        keep = s >= thr
        pb, pl = z["boxes"][k][keep], lab[keep]
        gts = gt_by_img.get(int(iid), [])
        area_m2 += img_px[int(iid)] * GSD_REF_M ** 2
        gb = np.array([g[:4] for g in gts]) if gts else np.zeros((0, 4))
        used = np.zeros(len(pb), bool)
        if len(gb) and len(pb):
            M = iou_xyxy(gb, pb)
        for gi, g in enumerate(gts):
            grp = "crop" if g[4] in CROP else "weed"
            size = float(np.sqrt((g[2] - g[0]) * (g[3] - g[1])))
            hit, right = False, False
            if len(pb):
                cand = np.where((M[gi] >= 0.5) & ~used)[0]
                if len(cand):
                    j = cand[np.argmax(M[gi, cand])]
                    used[j] = True
                    hit = True
                    pgrp = "crop" if classes[pl[j]] in CROP else "weed"
                    right = pgrp == grp
                    conf[f"{grp}_n"] += 1
                    if not right:
                        conf["weed_as_crop" if grp == "weed" else "crop_as_weed"] += 1
            det[grp].append((size, hit, right))
        for j in np.where(~used)[0]:
            fp_total += 1
            if classes[pl[j]] not in CROP:
                fp_w += 1
        tp_w += sum(1 for j in np.where(used)[0] if classes[pl[j]] not in CROP)

    def by_size(rows):
        rows = np.array(rows, dtype=float) if rows else np.zeros((0, 3))
        rec, n, rec_right = [], [], []
        for lo, hi in zip(SIZE_BINS[:-1], SIZE_BINS[1:]):
            sel = (rows[:, 0] >= lo) & (rows[:, 0] < hi) if len(rows) else np.zeros(0, bool)
            n.append(int(sel.sum()))
            rec.append(float(rows[sel, 1].mean()) if sel.any() else None)
            rec_right.append(float(rows[sel, 2].mean()) if sel.any() else None)
        med = float(np.median(rows[:, 0])) if len(rows) else None
        return {"recall_any": rec, "recall_right_group": rec_right, "n": n, "median_size_px": med,
                "overall_recall_right_group": float(rows[:, 2].mean()) if len(rows) else None}

    return {
        "model": metrics["model_version"], "threshold": thr, "split": split,
        "gsd_ref_m": GSD_REF_M, "size_bins_px": SIZE_BINS,
        "weed": by_size(det["weed"]), "crop": by_size(det["crop"]),
        "weed_precision": tp_w / max(tp_w + fp_w, 1),
        "fp_per_m2": fp_total / max(area_m2, 1e-9), "weed_fp_per_m2": fp_w / max(area_m2, 1e-9),
        "weed_as_crop_frac": conf["weed_as_crop"] / max(conf["weed_n"], 1),
        "crop_as_weed_frac": conf["crop_as_weed"] / max(conf["crop_n"], 1),
        "provenance": ("RF-DETR-Medium fine-tuned on WeedsGalore (CC-BY-4.0) on the H200, 30 Oct 2026; "
                       f"{split} split, IoU 0.5 greedy matching, operating threshold {thr}. One German "
                       "maize field at ~2.5 mm GSD: recalibrate on field labels before trusting elsewhere."),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    res = compute(a.run, a.data)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1))
    print(json.dumps({k: res[k] for k in ("weed_precision", "fp_per_m2", "weed_as_crop_frac",
                                          "crop_as_weed_frac")}, indent=1))
    print("weed recall by size:", res["weed"]["recall_right_group"], res["weed"]["n"])
