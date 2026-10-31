"""Compare conformal gates on a trained detector's cached scores.

The H200 run measured a *marginal* LAC gate: coverage 0.923 at alpha=0.10, but
quickweed coverage 0.52 at alpha=0.05 and a 59.7 % review rate. This script
answers, on the same real scores, whether:

1. a **class-conditional** gate restores per-class coverage (and at what
   review cost), and
2. an **action-level** gate (crop / weed / background) -- all a spray decision
   needs -- cuts the review rate as the simulation predicted (78.7 % -> 2.5 %).

It needs no GPU: it reads the predictions ``ml/evaluate.py`` cached in
``<run>/eval/preds_{valid,test}_torch.npz`` and the COCO annotations.

    python ml/gate_eval.py --run ml/runs/wg-medium --data ~/data/weedsgalore_coco
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "fa_core"))
sys.path.insert(0, str(ROOT / "ml"))

from fa_core.uncertainty import (action_grouping, calibrate_action_gate,  # noqa: E402
                                 calibrate_class_conditional, calibrate_classification,
                                 group_labels)


def _boot_ci(cover: np.ndarray, groups: np.ndarray | None, n_boot: int = 2000, seed: int = 0):
    if groups is None or len(groups) != len(cover) or len(cover) == 0:
        return None
    uniq, inv = np.unique(groups, return_inverse=True)
    hit = np.bincount(inv, weights=cover.astype(float), minlength=len(uniq))
    cnt = np.bincount(inv, minlength=len(uniq)).astype(float)
    idx = np.random.default_rng(seed).integers(0, len(uniq), (n_boot, len(uniq)))
    boot = hit[idx].sum(1) / np.clip(cnt[idx].sum(1), 1, None)
    return [round(float(np.quantile(boot, 0.025)), 4), round(float(np.quantile(boot, 0.975)), 4)]


def _stats(sets: list[list[str]], truth: list[str], names: list[str], groups=None) -> dict:
    cover = np.array([t in s for t, s in zip(truth, sets)])
    size = np.array([len(s) for s in sets])
    per = {c: {"n": int(sum(t == c for t in truth)),
               "coverage": round(float(cover[np.array([t == c for t in truth])].mean()), 4)}
           for c in names if any(t == c for t in truth)}
    return {"n": len(truth), "coverage": round(float(cover.mean()), 4),
            "coverage_ci95": _boot_ci(cover, groups), "mean_set_size": round(float(size.mean()), 3),
            "review_rate": round(float((size != 1).mean()), 4),
            "worst_class_coverage": round(min(v["coverage"] for v in per.values()), 4),
            "per_class": per}


def evaluate_gates(val_p: np.ndarray, val_l: np.ndarray, test_p: np.ndarray, test_l: np.ndarray,
                   classes: list[str], alpha: float, test_groups: np.ndarray | None = None) -> dict:
    """Marginal LAC vs class-conditional vs action-level gates on held-out detections."""
    truth = [classes[int(y)] for y in test_l]
    marg = calibrate_classification(val_p, val_l, classes, alpha=alpha, score="lac", randomized=False)
    mond = calibrate_class_conditional(val_p, val_l, classes, alpha=alpha)
    groups = action_grouping(classes)
    gate = calibrate_action_gate(val_p, val_l, classes, alpha=alpha, groups=groups)
    gnames = list(groups)
    gtruth = [gnames[g] for g in group_labels(test_l, classes, groups)]
    return {
        "alpha": alpha,
        "marginal_lac": _stats([marg.predict_set(p) for p in test_p], truth, classes, test_groups),
        "class_conditional": {**_stats([mond.predict_set(p) for p in test_p], truth, classes, test_groups),
                              "undercalibrated": mond.undercalibrated},
        "action_level": {**_stats([gate.predict(p) for p in test_p], gtruth, gnames, test_groups),
                         "groups": groups, "undercalibrated": gate.cal.undercalibrated},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--data", type=Path, help="COCO dir (default: from run.json)")
    ap.add_argument("--backend", default="torch")
    ap.add_argument("--alpha", type=float, nargs="+", default=[0.05, 0.10, 0.20])
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    from fa_ml import detconf
    run = json.loads((a.run / "run.json").read_text()) if (a.run / "run.json").exists() else {}
    data = a.data or Path(run.get("data", ""))
    val, test = detconf.Split.load(data, "valid"), detconf.Split.load(data, "test")
    pv = detconf.Preds.load(a.run / "eval" / f"preds_valid_{a.backend}.npz")
    pt = detconf.Preds.load(a.run / "eval" / f"preds_test_{a.backend}.npz")
    thr = None
    for mp in (a.run / "eval" / "metrics.json", ROOT / "ml" / "results" / a.run.name / "metrics.json"):
        if mp.exists():
            thr = json.loads(mp.read_text()).get("operating_threshold")
            break
    thr = float(thr if thr is not None else detconf.choose_threshold(val, pv)[0])
    classes = val.classes + ["background"]
    vp, vl, _, _ = detconf.detections(val, pv, thr)
    tp, tl, _, _ = detconf.detections(test, pt, thr)
    groups = detconf.detection_images(test, pt, thr)
    out = {"run": str(a.run), "threshold": thr, "classes": classes,
           "n_val": int(len(vl)), "n_test": int(len(tl)),
           "by_alpha": {str(al): evaluate_gates(vp, vl, tp, tl, classes, al, groups) for al in a.alpha}}
    dest = a.out or (ROOT / "ml" / "results" / a.run.name / "gates.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1))
    for al, r in out["by_alpha"].items():
        print(f"alpha={al}")
        for k in ("marginal_lac", "class_conditional", "action_level"):
            x = r[k]
            print(f"  {k:18s} coverage {x['coverage']:.3f}  worst class {x['worst_class_coverage']:.3f}  "
                  f"review {x['review_rate']:.1%}  set {x['mean_set_size']:.2f}")
    print("->", dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
