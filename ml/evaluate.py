#!/usr/bin/env python3
"""Evaluate a trained detector and calibrate its conformal gate on real images.

    python ml/evaluate.py --run ml/runs/wg-medium
    python ml/evaluate.py --run ml/runs/wg-medium --backend onnx     # after export.py

Steps (all through fa_gateway.rfdetr, the code the aircraft runs):
  1. predict every query on valid/ and test/ (cached in <run>/eval/preds_*.npz)
  2. COCO mAP on test
  3. operating threshold = best F1 on *valid*
  4. LAC conformal gate calibrated on valid, measured on test, alpha 0.05/0.10/0.20
  5. per-date strata (the gateway's StratifiedCalibrator) and a date-shift test
  6. writes <run>/eval/{metrics.json, report.md, calibration.json, *.png}, the
     model card, and copies the publishable files to ml/results/<run>/

Nothing here is tuned on test: test is touched once, after every choice is made
on valid.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fa_ml import ROOT  # noqa: E402
from fa_ml import detconf  # noqa: E402
from fa_ml.env import describe  # noqa: E402

ALPHAS = (0.05, 0.10, 0.20)
INK, INK2, MUTED, GRID, SURF = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0", "#fcfcfb"
BLUE, ORANGE = "#2a78d6", "#eb6834"


def _fig_coverage(rows: list[dict], out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6.4, 5.2), dpi=150)
    fig.patch.set_facecolor(SURF)
    ax.set_facecolor(SURF)
    ax.plot([0.75, 1.0], [0.75, 1.0], color=MUTED, lw=1.2, ls="--", zorder=1)
    ax.text(0.985, 0.972, "target", color=MUTED, fontsize=9, ha="right", rotation=38)
    for r in rows:
        nom, cov = 1 - r["alpha"], r["test"]["coverage"]
        ax.scatter([nom], [cov], s=70, color=BLUE, edgecolor=SURF, linewidth=2, zorder=3)
        ax.annotate(f"α={r['alpha']:.2f}\ncoverage {cov:.3f}\nreview {r['test']['review_rate']:.0%}",
                    (nom, cov), textcoords="offset points", xytext=(10, -34), fontsize=8.5, color=INK2)
    ax.set_xlim(0.75, 1.0)
    ax.set_ylim(0.75, 1.0)
    ax.set_xlabel("nominal coverage (1 − α)", color=INK2)
    ax.set_ylabel("measured coverage on test", color=INK2)
    ax.set_title("Conformal gate on real UAV plant detections", color=INK, fontsize=11, loc="left")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.grid(color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out, facecolor=SURF)
    plt.close(fig)


def _fig_shift(shift: dict, strat: dict, alpha: float, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    dates = sorted(shift["by_date"])
    fig, ax = plt.subplots(figsize=(7.2, 4.4), dpi=150)
    fig.patch.set_facecolor(SURF)
    ax.set_facecolor(SURF)
    x = np.arange(len(dates))
    w = 0.36
    a = [shift["by_date"][d]["coverage"] for d in dates]
    b = [strat.get(d, {}).get("coverage", np.nan) for d in dates]
    ax.bar(x - w / 2 - 0.01, a, w, color=ORANGE, edgecolor=SURF, linewidth=2,
           label="calibrated on early dates only")
    ax.bar(x + w / 2 + 0.01, b, w, color=BLUE, edgecolor=SURF, linewidth=2,
           label="calibrated per date (stratified)")
    ax.axhline(1 - alpha, color=INK2, lw=1.2, ls="--")
    ax.text(len(dates) - 0.5, 1 - alpha + 0.004, f"target {1 - alpha:.2f}", color=INK2,
            fontsize=8.5, ha="right", va="bottom")
    for i, d in enumerate(dates):
        ax.text(i - w / 2 - 0.01, a[i] + 0.004, f"{a[i]:.3f}", ha="center", va="bottom", fontsize=8, color=INK)
        if shift["by_date"][d]["alarm"]:
            ax.text(i - w / 2 - 0.01, a[i] - 0.02, "alarm", ha="center", va="top", fontsize=8,
                    color=SURF, weight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([d + ("\n(seen)" if shift["by_date"][d]["seen_in_calibration"] else "\n(later stage)")
                        for d in dates], color=INK2, fontsize=8.5)
    lo = min([v for v in a + b if v == v] + [1 - alpha]) - 0.08
    ax.set_ylim(max(0.0, lo), 1.0)
    ax.set_ylabel("coverage on test", color=INK2)
    ax.set_title("Growth-stage shift: does the gate hold on dates it was not calibrated on?",
                 color=INK, fontsize=10.5, loc="left")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.yaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=8.5, loc="lower left")
    fig.tight_layout()
    fig.savefig(out, facecolor=SURF)
    plt.close(fig)


def model_card(run: dict, m: dict) -> str:
    dm = run.get("dataset_meta") or {}
    c10 = next(r for r in m["conformal"] if abs(r["alpha"] - 0.10) < 1e-9)
    tax = {"maize": "crop", "amaranth": "broadleaf_weed", "quickweed": "broadleaf_weed",
           "barnyard_grass": "grass_weed", "weed_other": "weed_other"}
    lines = [
        f"# Model card: {run['model_version']}", "",
        "| | |", "|---|---|",
        f"| Architecture | RF-DETR-{run['size']} (Roboflow, Apache-2.0 code and weights) at {run['resolution']} px |",
        f"| Training data | {dm.get('dataset')} ({dm.get('licence')}) — {dm.get('citation')} |",
        f"| Classes | {', '.join(run['classes'])} + background |",
        f"| Future Agri taxonomy | " + "; ".join(f"{k} → {v}" for k, v in tax.items() if k in run["classes"]) + " |",
        f"| Trained on | {run['env'].get('gpu') or 'CPU'}, torch {run['env'].get('torch')}, "
        f"{run.get('train_minutes', '?')} min, git {run['env'].get('git')} |",
        f"| Synthetic data | {'YES — smoke run, not a model' if dm.get('synthetic') or run.get('smoke') else 'no'} |",
        "", "## Results (test split, never used for any choice)", "",
        f"* COCO mAP@[.5:.95] **{m['detection']['map'].get('map50_95')}**, AP50 "
        f"**{m['detection']['map'].get('map50')}**",
        f"* At the operating threshold {m['operating_threshold']} (best F1 on valid): precision "
        f"{m['detection']['at_threshold']['precision']}, recall {m['detection']['at_threshold']['recall']}",
        f"* Conformal gate, α = 0.10: coverage **{c10['test']['coverage']}** (target 0.90), mean set "
        f"size {c10['test']['mean_set_size']}, review rate **{c10['test']['review_rate']:.1%}**", "",
        "## Intended use", "",
        "Per-plant crop/weed detection on nadir UAV RGB at millimetre GSD, feeding the Future Agri "
        "edge gateway, which routes every non-singleton conformal set to human review.", "",
        "## Limitations", "",
        "* Trained on one German maize site, four dates in May–June 2023, 2.5 mm GSD. It has never "
        "seen Indian fields, other crops, other soils or other altitudes; recalibrate (and expect to "
        "retrain) before any use elsewhere. The date-shift test in `report.md` shows what happens "
        "when growth stage changes.",
        "* RGB composites built from the multispectral camera's R, G, B bands, not a consumer RGB "
        "camera; colour differs.",
        "* Detects plants, not disease or nutrient stress.",
        "* Coverage is guaranteed only under exchangeability with the calibration data; the gateway "
        "monitors it and alarms when it drops.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--data", type=Path, help="COCO dir (default: the one in run.json)")
    ap.add_argument("--backend", default="torch", choices=("torch", "onnx", "tensorrt"))
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--min-stratum", type=int, default=200)
    ap.add_argument("--refresh", action="store_true", help="ignore cached predictions")
    a = ap.parse_args()

    run = json.loads((a.run / "run.json").read_text(encoding="utf-8"))
    data = a.data or Path(run["dataset"])
    ev = a.run / "eval"
    ev.mkdir(exist_ok=True)
    from fa_gateway.rfdetr import BACKGROUND, detector_from_run
    det = detector_from_run(a.run, backend=a.backend, threshold=0.0)
    print(f"evaluating {run['model_version']} with {det.model_version}")

    splits, preds = {}, {}
    for name in ("valid", "test"):
        splits[name] = detconf.Split.load(data, name)
        cache = ev / f"preds_{name}_{a.backend}.npz"
        if cache.exists() and not a.refresh:
            preds[name] = detconf.Preds.load(cache)
        else:
            preds[name] = detconf.predict_split(det, splits[name])
            preds[name].save(cache)
    classes = splits["test"].classes + [BACKGROUND]
    assert splits["test"].classes == run["classes"], "dataset classes differ from the trained model's"

    print("  COCO mAP ...")
    mp = detconf.coco_map(splits["test"], preds["test"])
    thr, sweep = detconf.choose_threshold(splits["valid"], preds["valid"])
    vP, vL, vD, vst = detconf.detections(splits["valid"], preds["valid"], thr, a.iou)
    tP, tL, tD, tst = detconf.detections(splits["test"], preds["test"], thr, a.iou)
    print(f"  threshold {thr} (valid F1 {vst['f1']}); test P {tst['precision']} R {tst['recall']}")

    tG = detconf.detection_images(splits["test"], preds["test"], thr)
    rows = []
    for alpha in ALPHAS:
        cal = detconf.calibrate(vP, vL, classes, alpha)
        rows.append({"alpha": alpha, "q": float(cal.q) if np.isfinite(cal.q) else None, "n_calibration": cal.n,
                     "certified": bool(np.isfinite(cal.q)),
                     "test": detconf.set_stats(cal, tP, tL, classes, groups=tG)})
        raps = detconf.calibrate(vP, vL, classes, alpha, score="raps")
        rows[-1]["test_raps"] = {k: v for k, v in detconf.set_stats(raps, tP, tL, classes).items()
                                 if k != "per_class"}
        r = rows[-1]["test"]
        print(f"  alpha {alpha:.2f}: coverage {r.get('coverage')} set {r.get('mean_set_size')} "
              f"review {r.get('review_rate')}")

    sc = detconf.stratified(vP, vL, vD, classes, 0.10, a.min_stratum)
    sc.to_json(ev / "calibration.json")
    strat = detconf.stratified_stats(sc, tP, tL, tD, classes)
    shift = detconf.shift_experiment((vP, vL, vD), (tP, tL, tD), classes, 0.10)

    metrics = {"run": run["name"], "model_version": run["model_version"], "backend": det.model_version,
               "operating_threshold": thr, "iou_match": a.iou,
               "detection": {"map": mp, "at_threshold": tst, "valid_at_threshold": vst,
                             "threshold_sweep_valid": sweep},
               "conformal": rows, "stratified_alpha_0.10": strat, "date_shift_alpha_0.10": shift,
               "calibration_file": "calibration.json", "env": describe()}
    (ev / "metrics.json").write_text(json.dumps(metrics, indent=1, default=float), encoding="utf-8")

    _fig_coverage(rows, ev / "coverage.png")
    if "by_date" in shift:
        _fig_shift(shift, strat, 0.10, ev / "shift.png")

    rep = [f"# {run['model_version']} — evaluation", "",
           f"Backend: `{det.model_version}` · threshold {thr} chosen on valid · IoU {a.iou}", "",
           "| metric | value |", "|---|---:|",
           f"| mAP@[.5:.95] (test) | {mp.get('map50_95')} |", f"| AP50 (test) | {mp.get('map50')} |",
           f"| precision / recall @ thr (test) | {tst['precision']} / {tst['recall']} |", "",
           "## Conformal gate (calibrated on valid, measured on test)", "",
           "| α | coverage | 95% CI (images) | target | mean set | review rate | alarm | RAPS coverage | RAPS review |",
           "|---:|---:|:---:|---:|---:|---:|:---:|---:|---:|"]
    for r in rows:
        t, rr = r["test"], r["test_raps"]
        ci = t.get("coverage_ci95") or ["–", "–"]
        rep.append(f"| {r['alpha']:.2f} | {t['coverage']} | {ci[0]}–{ci[1]} | {1 - r['alpha']:.2f} | {t['mean_set_size']} | "
                   f"{t['review_rate']:.1%} | {'yes' if t['alarm'] else 'no'} | {rr['coverage']} | "
                   f"{rr['review_rate']:.1%} |")
    rep += ["", "![coverage](coverage.png)", "", "## Growth-stage shift (α = 0.10)", "",
            "| test date | early-only calibration | alarm | per-date calibration |", "|---|---:|:---:|---:|"]
    for d, r in (shift.get("by_date") or {}).items():
        rep.append(f"| {d}{' (seen)' if r['seen_in_calibration'] else ''} | {r['coverage']} | "
                   f"{'**yes**' if r['alarm'] else 'no'} | {strat.get(d, {}).get('coverage')} |")
    rep += ["", "![shift](shift.png)", ""]
    (ev / "report.md").write_text("\n".join(rep) + "\n", encoding="utf-8")
    (a.run / "MODEL_CARD.md").write_text(model_card(run, metrics), encoding="utf-8")

    pub = ROOT / "ml" / "results" / run["name"]
    pub.mkdir(parents=True, exist_ok=True)
    for f in ("metrics.json", "report.md", "calibration.json", "coverage.png", "shift.png"):
        if (ev / f).exists():
            shutil.copy2(ev / f, pub / f)
    shutil.copy2(a.run / "MODEL_CARD.md", pub / "MODEL_CARD.md")
    shutil.copy2(a.run / "run.json", pub / "run.json")
    print(f"\n-> {ev / 'report.md'}\n-> {pub} (commit this folder)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
