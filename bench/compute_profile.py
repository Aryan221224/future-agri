#!/usr/bin/env python3
"""What compute does this project actually need?

Answering this properly matters, because the instinct is to ask for the biggest
GPU available and that is the wrong request. The two workloads have opposite
requirements:

  crop simulation  -- CPU-bound, single-threaded per run, embarrassingly
                      parallel across runs. A GPU contributes NOTHING. What it
                      wants is cores and RAM.
  detector training -- GPU-bound, but a modest one. 8-16 GB VRAM is plenty for
                      a few thousand annotated field images.

This measures the first directly, because it is the part that scales with the
planner's scenario count and therefore with the quality of the decision.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "twin"))

BASE = dict(latitude=28.4506, longitude=77.5842, crop="potato",
            sowing_date=dt.date(2026, 10, 15), harvest_date=dt.date(2027, 2, 10),
            WAV=30.0)


def _one(seed: int) -> float:
    import numpy as np
    from fa_twin.engine import ZoneConfig, ZoneTwin, variety_support
    from fa_twin.weather import make_synthetic_weather

    v = variety_support("potato", "Wofost72_WLP_CWB")[0]
    w = make_synthetic_weather(BASE["latitude"], BASE["longitude"],
                               dt.date(2026, 10, 1), days=180, seed=seed % 32)
    tw = ZoneTwin(ZoneConfig(zone_id=f"s{seed}", variety=v, **BASE), w,
                  engine="Wofost72_WLP_CWB")
    rng = np.random.default_rng(seed)
    pp = tw.model.parameterprovider
    for p in ("TSUM1", "TSUM2", "SPAN", "RGRLAI"):
        try:
            pp.set_override(p, float(pp[p]) * float(np.exp(rng.normal(0, 0.2))),
                            check=False)
        except Exception:
            pass
    tw.run_to_end()
    return float(tw.summary().get("TWSO") or 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=64)
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / "compute.json")
    a = ap.parse_args()
    cores = os.cpu_count() or 1

    t0 = time.perf_counter()
    serial = [_one(i) for i in range(min(a.runs, 16))]
    t_serial = (time.perf_counter() - t0) / len(serial)

    results = {"cores_available": cores, "seconds_per_simulation_serial": t_serial}
    print(f"cores available here: {cores}")
    print(f"one season simulation: {t_serial * 1000:.0f} ms single-threaded\n")

    speedups = {}
    for workers in sorted({1, 2, max(1, cores // 2), cores}):
        t0 = time.perf_counter()
        with ProcessPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_one, range(a.runs)))
        el = time.perf_counter() - t0
        rate = a.runs / el
        speedups[workers] = rate
        print(f"  {workers:2d} worker(s): {a.runs} sims in {el:5.1f} s "
              f"= {rate:6.1f} sims/s")
    results["sims_per_second_by_workers"] = speedups

    best = max(speedups.values())
    print(f"\nWhat that means for the planner:")
    for label, n in (("one decision (5 scenarios x 5 epochs, beam-4)", 260),
                     ("a 100-member assimilation ensemble", 100),
                     ("a 12-trial OSSE validation", 12 * 2 * 30),
                     ("10,000-run sensitivity sweep for the paper", 10_000)):
        print(f"  {label:52s} {n:6d} sims -> {n / best / 60:7.1f} min on {cores} cores")

    print("\nProjection onto machines you might actually get:")
    per_core = speedups.get(1, best)
    for name, c in (("laptop, 8 cores", 8), ("workstation, 16 cores", 16),
                    ("HPC CPU node, 64 cores", 64), ("HPC CPU node, 128 cores", 128)):
        rate = per_core * c * 0.85     # 85% parallel efficiency
        print(f"  {name:26s} ~{rate:6.1f} sims/s -> 10,000 runs in "
              f"{10_000 / rate / 60:5.1f} min")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nwritten to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
