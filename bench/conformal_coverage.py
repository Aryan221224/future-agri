#!/usr/bin/env python3
"""Does the conformal layer deliver the coverage it claims, and at what cost?

Coverage is the guarantee. Set size and review rate are the price. A method that
achieves nominal coverage by sending everything to a human has not solved
anything, so both are reported together.

Also measures what happens when exchangeability breaks -- which in agriculture
is not a hypothetical but the normal condition of a new season.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "fa_core"))

from fa_core.uncertainty import (calibrate_classification, calibrate_regression,  # noqa: E402
                                 coverage_alarm, empirical_coverage)


def softmax_data(rng, n, k, sep_lo=0.5, sep_hi=3.0):
    y = rng.integers(0, k, n)
    lg = rng.normal(0, 1, (n, k))
    lg[np.arange(n), y] += rng.uniform(sep_lo, sep_hi, n)
    e = np.exp(lg - lg.max(1, keepdims=True))
    return e / e.sum(1, keepdims=True), y


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--classes", type=int, default=6)
    ap.add_argument("--calib", type=int, default=4000)
    ap.add_argument("--test", type=int, default=20000)
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / "conformal.json")
    a = ap.parse_args()

    rng = np.random.default_rng(0)
    classes = [f"c{i}" for i in range(a.classes)]
    rows = []

    print("classification -- coverage against the price paid for it")
    print(f"{'score':6s} {'mode':14s} {'alpha':>6s} {'coverage':>9s} {'mean|set|':>10s} {'review':>8s}")
    for alpha in (0.05, 0.10, 0.20):
        p, y = softmax_data(rng, a.calib, a.classes)
        pt, yt = softmax_data(rng, a.test, a.classes)
        u = rng.uniform(size=len(yt))
        for score in ("lac", "raps"):
            cal = calibrate_classification(p, y, classes, alpha=alpha, score=score)
            for mode, us in (("deterministic", [None] * len(yt)), ("randomized", u)):
                sets = [cal.predict_set(r, uu) for r, uu in zip(pt, us)]
                cov = float(np.mean([classes[t] in s for s, t in zip(sets, yt)]))
                size = float(np.mean([len(s) for s in sets]))
                rev = float(np.mean([len(s) != 1 for s in sets]))
                rows.append(dict(kind="classification", score=score, mode=mode,
                                 alpha=alpha, coverage=cov, mean_set_size=size,
                                 review_rate=rev))
                print(f"{score:6s} {mode:14s} {alpha:6.2f} {cov:9.4f} {size:10.2f} {rev:7.1%}")

    print("\nregression -- constant vs normalized interval width")
    reg_rows = []
    for alpha in (0.05, 0.10, 0.20):
        t = rng.uniform(0, 6, a.calib)
        s = 0.10 + 0.25 * t
        pr = t + rng.normal(0, s)
        c_const = calibrate_regression(t, pr, alpha=alpha)
        c_norm = calibrate_regression(t, pr, alpha=alpha, sigma=s)
        tt = rng.uniform(0, 6, a.test)
        ss = 0.10 + 0.25 * tt
        pp = tt + rng.normal(0, ss)
        cov_c = empirical_coverage(tt, pp - c_const.q, pp + c_const.q)
        cov_n = empirical_coverage(tt, pp - c_norm.q * ss, pp + c_norm.q * ss)
        w_c, w_n = 2 * c_const.q, float(np.mean(2 * c_norm.q * ss))
        reg_rows.append(dict(alpha=alpha, coverage_constant=cov_c, coverage_normalized=cov_n,
                             width_constant=w_c, width_normalized=w_n))
        print(f"  alpha={alpha:.2f}  constant cov {cov_c:.4f} width {w_c:.3f} | "
              f"normalized cov {cov_n:.4f} width {w_n:.3f}")

    print("\nexchangeability violation -- a new season the calibration never saw")
    shift_rows = []
    p, y = softmax_data(rng, a.calib, a.classes)
    cal = calibrate_classification(p, y, classes, alpha=0.10, score="lac")
    for label, lo, hi in (("in-distribution", 0.5, 3.0),
                          ("mild shift", 0.2, 2.0),
                          ("severe shift", 0.0, 1.0)):
        pt, yt = softmax_data(rng, a.test, a.classes, lo, hi)
        sets = [cal.predict_set(r) for r in pt]
        cov = float(np.mean([classes[t] in s for s, t in zip(sets, yt)]))
        alarm = coverage_alarm(cov, 0.90, len(yt))
        shift_rows.append(dict(regime=label, coverage=cov, alarm=alarm))
        print(f"  {label:16s} coverage {cov:.4f}   monitor alarm: "
              f"{'FIRES' if alarm else 'quiet'}")

    out = {"classification": rows, "regression": reg_rows, "distribution_shift": shift_rows,
           "note": ("Deterministic RAPS over-covers, which inflates set size and "
                    "therefore human review load. LAC is the production default: "
                    "near-exact deterministically, smallest sets.")}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwritten to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
