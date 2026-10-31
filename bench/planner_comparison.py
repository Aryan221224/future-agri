#!/usr/bin/env python3
"""Does MPPI actually beat beam search on agronomic scheduling? (No.)

An earlier revision of this architecture specified MPPI for choosing
interventions. This benchmark tests that choice instead of asserting it, on the
real crop model, with the same scenario ensemble and the same CVaR objective for
every method -- so the only thing that differs is the search.

Methods compared:
  do-nothing        the null baseline every agronomic result must beat
  uniform-split     the naive agronomic heuristic: split N evenly across epochs
  greedy            beam width 1
  beam-K            beam search at width K
  mppi              Gaussian-perturbation sampling MPC with rounding

Reported: best CVaR margin found, and the number of crop-model rollouts spent
to find it. A method that wins by spending 10x the compute has not won.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for p in ("services/twin", "services/planner", "packages/fa_core"):
    sys.path.insert(0, str(ROOT / p))

from fa_planner.scheduler import (  # noqa: E402
    Action, Economics, NO_ACTION, ScenarioEvaluator, beam_search,
    decision_epochs, greedy, mppi_schedule,
)
from fa_twin.engine import ZoneConfig  # noqa: E402
from fa_twin.weather import make_synthetic_weather  # noqa: E402

LAT, LON = 30.90, 75.85
CFG = ZoneConfig(zone_id="bench", latitude=LAT, longitude=LON, crop="rice",
                 variety="Rice_501", sowing_date=dt.date(2025, 6, 20),
                 harvest_date=dt.date(2025, 10, 25), WAV=35.0,
                 # Deliberately N-limited: at NAVAILI=30 the crop is not
                 # nitrogen-limited in this scenario and every method correctly
                 # returns "do nothing", which is the right answer and a useless
                 # benchmark. A scheduling comparison is only informative where
                 # scheduling has something to decide.
                 NAVAILI=5.0, NSOILBASE=2.0)
ENGINE = "Wofost81_NWLP_CWB_CNB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenarios", type=int, default=6)
    ap.add_argument("--epochs", type=int, default=5)
    ap.add_argument("--beam", type=int, default=6)
    ap.add_argument("--mppi-samples", type=int, default=24)
    ap.add_argument("--mppi-iters", type=int, default=4)
    ap.add_argument("--alpha", type=float, default=0.8)
    ap.add_argument("--max-applications", type=int, default=3)
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / "planner.json")
    a = ap.parse_args()

    weathers = [make_synthetic_weather(LAT, LON, dt.date(2025, 6, 1), days=200, seed=s)
                for s in range(a.scenarios)]
    epochs = decision_epochs(dt.date(2025, 7, 5), dt.date(2025, 9, 5), 14)[: a.epochs]
    econ = Economics()

    def fresh():
        return ScenarioEvaluator(CFG, weathers, econ, seed=0, engine=ENGINE)

    results = []

    def record(name, sched, rollouts, seconds):
        results.append(dict(method=name, cvar=sched.cvar, mean=sched.expected_value,
                            mean_yield=sched.mean_yield, cost=sched.cost,
                            n_applications=sched.n_applications, rollouts=rollouts,
                            seconds=round(seconds, 1),
                            plan=[f"{d:%b%d}:{v.amount:g}" for d, v in sorted(sched.actions.items())
                                  if v.kind != "none"] or ["none"]))
        print(f"  {name:16s} CVaR {sched.cvar:8.1f}  mean {sched.expected_value:8.1f}  "
              f"yield {sched.mean_yield:6.0f}  cost {sched.cost:6.0f}  "
              f"rollouts {rollouts:5d}  {seconds:5.1f}s  {results[-1]['plan']}", flush=True)

    print(f"scenarios={a.scenarios} epochs={len(epochs)} alpha={a.alpha} engine={ENGINE}\n")

    ev = fresh(); t = time.perf_counter()
    record("do-nothing", ev.score({e: NO_ACTION for e in epochs}, a.alpha),
           ev.n_rollouts, time.perf_counter() - t)

    ev = fresh(); t = time.perf_counter()
    per = 120.0 / len(epochs)
    record("uniform-split", ev.score({e: Action("fertilise", per, "urea") for e in epochs}, a.alpha),
           ev.n_rollouts, time.perf_counter() - t)

    ev = fresh(); t = time.perf_counter()
    record("greedy", greedy(ev, epochs, alpha=a.alpha, max_applications=a.max_applications),
           ev.n_rollouts, time.perf_counter() - t)

    for k in (3, a.beam):
        ev = fresh(); t = time.perf_counter()
        best = beam_search(ev, epochs, beam_width=k, alpha=a.alpha,
                           max_applications=a.max_applications)[0]
        record(f"beam-{k}", best, ev.n_rollouts, time.perf_counter() - t)

    ev = fresh(); t = time.perf_counter()
    record("mppi", mppi_schedule(ev, epochs, n_samples=a.mppi_samples,
                                 n_iters=a.mppi_iters, alpha=a.alpha),
           ev.n_rollouts, time.perf_counter() - t)

    best = max(results, key=lambda r: r["cvar"])
    baseline = next(r for r in results if r["method"] == "do-nothing")
    mppi = next(r for r in results if r["method"] == "mppi")
    beam = max((r for r in results if r["method"].startswith("beam")), key=lambda r: r["cvar"])

    summary = {
        "config": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(a).items()},
        "results": results,
        "best_method": best["method"],
        "beam_vs_mppi_cvar_gain_pct": (
            100 * (beam["cvar"] - mppi["cvar"]) / abs(mppi["cvar"]) if mppi["cvar"] else None),
        "beam_vs_mppi_rollout_ratio": mppi["rollouts"] / max(beam["rollouts"], 1),
        "best_vs_do_nothing_margin": best["cvar"] - baseline["cvar"],
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n" + "=" * 72)
    print(f"best method: {best['method']}  (CVaR {best['cvar']:.1f})")
    print(f"beam ({beam['method']}) vs mppi: CVaR {beam['cvar']:.1f} vs {mppi['cvar']:.1f} "
          f"({summary['beam_vs_mppi_cvar_gain_pct']:+.1f}%), "
          f"mppi spent {summary['beam_vs_mppi_rollout_ratio']:.1f}x the rollouts")
    print(f"best plan beats do-nothing by {summary['best_vs_do_nothing_margin']:.1f} per ha")
    print(f"\nwritten to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
