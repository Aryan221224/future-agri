"""Detection accuracy and conformal calibration on real imagery.

What is measured
----------------
* **Detection**: COCO mAP@[.5:.95] and AP50, and precision / recall / F1 at the
  operating threshold chosen on the validation split (never on test).
* **The gate**: every detection above the threshold is labelled with the class
  of the plant it matches (IoU >= 0.5), or ``background`` if it matches none.
  A LAC split-conformal calibrator is fitted on validation detections; on test
  we report coverage (does the set contain the truth?), mean set size, and the
  review rate (share of detections whose set is not a singleton) -- the cost
  that decides whether anyone keeps using the system.
* **Strata**: one calibration per acquisition date (the growth-stage stratum),
  with the global calibration as the explicit fallback -- ``StratifiedCalibrator``
  from ``fa_core.uncertainty``, the object the gateway loads.
* **Shift**: calibrate on the earliest dates only, test on the latest. This is
  the exchangeability failure agriculture produces every season; the
  synthetic benchmark (``bench/conformal_coverage.py``) predicts coverage drops
  and the monitor fires. This measures whether it does on real plants.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from fa_core.uncertainty import (ClassificationCalibration, StratifiedCalibrator,
                                 calibrate_classification, coverage_alarm)

from .matching import label_detections

CROP, SITE = "maize", "weedsgalore-de"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Split:
    name: str
    dir: Path
    classes: list[str]
    images: list[dict]
    gt: dict[int, tuple[np.ndarray, np.ndarray]]      # image_id -> (xyxy (M,4), label 0..K-1)
    cat_to_label: dict[int, int]

    @classmethod
    def load(cls, coco_dir: Path, name: str) -> "Split":
        d = Path(coco_dir) / name
        coco = json.loads((d / "_annotations.coco.json").read_text(encoding="utf-8"))
        cats = sorted(coco["categories"], key=lambda c: c["id"])
        cat_to_label = {c["id"]: i for i, c in enumerate(cats)}
        gt: dict[int, list] = {im["id"]: [] for im in coco["images"]}
        for a in coco["annotations"]:
            x, y, w, h = a["bbox"]
            gt[a["image_id"]].append((x, y, x + w, y + h, cat_to_label[a["category_id"]]))
        arr = {k: (np.array([r[:4] for r in v], np.float32).reshape(-1, 4),
                   np.array([r[4] for r in v], np.int64)) for k, v in gt.items()}
        return cls(name, d, [c["name"] for c in cats], coco["images"], arr, cat_to_label)


@dataclass
class Preds:
    """All queries of every image in a split, from one detector. Cached to .npz."""
    image_ids: np.ndarray            # (I,)
    dates: list[str]
    boxes: np.ndarray                # (I, Q, 4) xyxy px
    class_scores: np.ndarray         # (I, Q, K)
    probs: np.ndarray                # (I, Q, K+1)

    def save(self, path: Path) -> None:
        np.savez_compressed(path, image_ids=self.image_ids, dates=np.array(self.dates),
                            boxes=self.boxes, class_scores=self.class_scores, probs=self.probs)

    @classmethod
    def load(cls, path: Path) -> "Preds":
        z = np.load(path, allow_pickle=False)
        return cls(z["image_ids"], [str(d) for d in z["dates"]], z["boxes"],
                   z["class_scores"], z["probs"])


def predict_split(detector, split: Split, progress: bool = True) -> Preds:
    """Run ``detector.queries`` (fa_gateway.rfdetr.RFDETRDetector) over a split."""
    from PIL import Image
    ids, dates, boxes, scores, probs = [], [], [], [], []
    for n, im in enumerate(split.images, start=1):
        img = np.asarray(Image.open(split.dir / im["file_name"]).convert("RGB"))
        q = detector.queries(img)
        ids.append(im["id"])
        dates.append(im.get("date", "unknown"))
        boxes.append(q.boxes_xyxy)
        scores.append(q.class_scores)
        probs.append(q.probs)
        if progress and (n % 10 == 0 or n == len(split.images)):
            print(f"    {split.name}: {n}/{len(split.images)} images")
    return Preds(np.array(ids), dates, np.stack(boxes), np.stack(scores), np.stack(probs))


# ---------------------------------------------------------------------------
# Detection metrics
# ---------------------------------------------------------------------------

def coco_map(split: Split, preds: Preds, max_dets: int = 100) -> dict:
    """COCO mAP from the top (query, class) pairs per image, as DETR evaluates."""
    try:
        from pycocotools.coco import COCO
        from pycocotools.cocoeval import COCOeval
    except ImportError:
        return {"error": "pycocotools not installed"}
    import contextlib
    import io
    label_to_cat = {v: k for k, v in split.cat_to_label.items()}
    res = []
    for i, iid in enumerate(preds.image_ids):
        s = preds.class_scores[i]                        # (Q, K)
        flat = np.argsort(-s.ravel())[:max_dets]
        q, k = np.unravel_index(flat, s.shape)
        for qi, ki in zip(q, k):
            x1, y1, x2, y2 = preds.boxes[i, qi]
            res.append({"image_id": int(iid), "category_id": int(label_to_cat[int(ki)]),
                        "bbox": [float(x1), float(y1), float(x2 - x1), float(y2 - y1)],
                        "score": float(s[qi, ki])})
    with contextlib.redirect_stdout(io.StringIO()):
        gt = COCO(str(split.dir / "_annotations.coco.json"))
        dt = gt.loadRes(res) if res else None
        if dt is None:
            return {"map50_95": 0.0, "map50": 0.0}
        ev = COCOeval(gt, dt, "bbox")
        ev.params.maxDets = [1, 10, max_dets]
        ev.evaluate()
        ev.accumulate()
        ev.summarize()
    out = {"map50_95": round(float(ev.stats[0]), 4), "map50": round(float(ev.stats[1]), 4),
           "map75": round(float(ev.stats[2]), 4), "map_small": round(float(ev.stats[3]), 4),
           "mar100": round(float(ev.stats[8]), 4), "per_class_ap50": {}}
    prec = ev.eval["precision"]                          # [T, R, K, A, M]
    for k_idx, cat_id in enumerate(ev.params.catIds):
        p = prec[0, :, k_idx, 0, -1]
        p = p[p > -1]
        out["per_class_ap50"][split.classes[split.cat_to_label[cat_id]]] = \
            round(float(p.mean()) if p.size else float("nan"), 4)
    return out


def detections(split: Split, preds: Preds, threshold: float, iou_thr: float = 0.5):
    """Flatten kept detections: probs (N,K+1), labels (N,), dates (N,), plus counts."""
    K = len(split.classes)
    P, L, D = [], [], []
    tp = fp = n_gt = 0
    for i, iid in enumerate(preds.image_ids):
        sc = preds.class_scores[i].max(1)
        keep = np.flatnonzero(sc >= threshold)
        gb, gl = split.gt[int(iid)]
        n_gt += len(gl)
        lab, m = label_detections(preds.boxes[i, keep], sc[keep], gb, gl, background_index=K,
                                  iou_thr=iou_thr)
        tp += int((m >= 0).sum())
        fp += int((m < 0).sum())
        P.append(preds.probs[i, keep])
        L.append(lab)
        D.extend([preds.dates[i]] * len(keep))
    P = np.concatenate(P) if P else np.zeros((0, K + 1), np.float32)
    L = np.concatenate(L) if L else np.zeros(0, np.int64)
    prec = tp / max(tp + fp, 1)
    rec = tp / max(n_gt, 1)
    return P, L, np.array(D), {"tp": tp, "fp": fp, "n_gt": n_gt, "precision": round(prec, 4),
                                "recall": round(rec, 4),
                                "f1": round(2 * prec * rec / max(prec + rec, 1e-9), 4)}


def detection_images(split: Split, preds: Preds, threshold: float) -> np.ndarray:
    """Image id of every detection, in the order ``detections`` returns them."""
    out = [np.full(int((preds.class_scores[i].max(1) >= threshold).sum()), int(iid))
           for i, iid in enumerate(preds.image_ids)]
    return np.concatenate(out) if out else np.zeros(0, np.int64)


def choose_threshold(split: Split, preds: Preds, grid=None) -> tuple[float, list[dict]]:
    """The operating threshold maximising class-agnostic F1 on *validation*."""
    grid = grid if grid is not None else np.round(np.arange(0.15, 0.76, 0.05), 2)
    rows = []
    for t in grid:
        _, _, _, st = detections(split, preds, float(t))
        rows.append({"threshold": float(t), **st})
    best = max(rows, key=lambda r: (r["f1"], -r["threshold"]))
    return best["threshold"], rows


# ---------------------------------------------------------------------------
# Conformal gate
# ---------------------------------------------------------------------------

def set_stats(cal: ClassificationCalibration, probs: np.ndarray, labels: np.ndarray,
              classes: list[str], groups: np.ndarray | None = None, n_boot: int = 2000,
              seed: int = 0) -> dict:
    """Coverage, set size and review rate of a calibrated gate on labelled detections.

    ``groups`` (the image of each detection) adds a 95 % interval for coverage
    from a bootstrap over *images*: detections in one frame share light,
    growth stage and focus, so they are not independent, and a per-detection
    interval would be too narrow -- with a few dozen test images, badly so.
    """
    if len(labels) == 0:
        return {"n": 0}
    sets = [cal.predict_set(p) for p in probs]
    truth = [classes[int(y)] for y in labels]
    cover = np.array([t in s for t, s in zip(truth, sets)])
    size = np.array([len(s) for s in sets])
    single = size == 1
    top = np.array([classes[int(np.argmax(p))] for p in probs])
    per_class = {}
    for c in classes:
        m = np.array([t == c for t in truth])
        if m.sum():
            per_class[c] = {"n": int(m.sum()), "coverage": round(float(cover[m].mean()), 4)}
    nominal = 1 - cal.alpha
    ci = None
    if groups is not None and len(groups) == len(labels):
        g = np.asarray(groups)
        uniq, inv = np.unique(g, return_inverse=True)
        hit = np.bincount(inv, weights=cover.astype(float), minlength=len(uniq))
        cnt = np.bincount(inv, minlength=len(uniq)).astype(float)
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, len(uniq), (n_boot, len(uniq)))
        boot = hit[idx].sum(1) / np.clip(cnt[idx].sum(1), 1, None)
        ci = [round(float(np.quantile(boot, 0.025)), 4), round(float(np.quantile(boot, 0.975)), 4)]
    return {"n": int(len(labels)), "n_images": int(len(np.unique(groups))) if groups is not None else None,
            "coverage": round(float(cover.mean()), 4), "coverage_ci95": ci,
            "mean_set_size": round(float(size.mean()), 3),
            "review_rate": round(float((~single).mean()), 4),
            "singleton_accuracy": round(float((top[single] == np.array(truth)[single]).mean()), 4)
            if single.any() else None,
            "top1_accuracy": round(float((top == np.array(truth)).mean()), 4),
            "alarm": bool(coverage_alarm(float(cover.mean()), nominal, len(labels))),
            "per_class": per_class}


def calibrate(probs, labels, classes, alpha: float, score: str = "lac",
              stratum: str = "global") -> ClassificationCalibration:
    return calibrate_classification(probs, labels, classes, alpha=alpha, score=score,
                                    randomized=False, stratum=stratum)


def stratified(probs, labels, dates, classes, alpha: float, min_n: int = 200) -> StratifiedCalibrator:
    sc = StratifiedCalibrator(alpha=alpha, min_n=min_n,
                              global_cal=calibrate(probs, labels, classes, alpha))
    for d in sorted(set(dates.tolist())):
        m = dates == d
        if m.sum() >= 1:
            key = StratifiedCalibrator.key(CROP, d, SITE)
            sc.strata[key] = calibrate(probs[m], labels[m], classes, alpha, stratum=key)
    return sc


def stratified_stats(sc: StratifiedCalibrator, probs, labels, dates, classes) -> dict:
    out = {}
    for d in sorted(set(dates.tolist())):
        m = dates == d
        cal, hit = sc.get(CROP, d, SITE)
        out[d] = {"calibrated_stratum": hit, **set_stats(cal, probs[m], labels[m], classes)}
    return out


def shift_experiment(val: tuple, test: tuple, classes, alpha: float, n_early: int = 2) -> dict:
    """Calibrate on the earliest ``n_early`` dates of val; test per date."""
    vp, vl, vd = val
    tp, tl, td = test
    dates = sorted(set(vd.tolist()) | set(td.tolist()))
    early = dates[:n_early]
    m = np.isin(vd, early)
    if m.sum() == 0:
        return {"error": "no early-date validation detections"}
    cal = calibrate(vp[m], vl[m], classes, alpha, stratum="early:" + ",".join(early))
    rows = {}
    for d in dates:
        mm = td == d
        if mm.sum():
            rows[d] = {"seen_in_calibration": d in early, **set_stats(cal, tp[mm], tl[mm], classes)}
    return {"calibrated_on": early, "n_calibration": int(m.sum()), "alpha": alpha, "by_date": rows}
