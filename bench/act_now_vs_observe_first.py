#!/usr/bin/env python3
"""Act now on a weak forecast, or observe for three weeks and act on a strong one?

Two candidate designs for the same system:

  A -- "mega simulator": capture once, initialise the twin, let a rich combined
       model (crop growth + resource use + weather forecast) compute forward,
       and act immediately.

  B -- "observe first": capture across three weeks with no drone deployment,
       hand the resulting trajectory to the sim, build the digital farm from
       what actually happened, and only then act.

B produces a better-informed decision -- bench/forecast_horizon.py measures how
much better. But it pays for that information with three weeks of delay, and in
agronomy a delayed intervention is often a weaker one: the crop has moved on,
and the window for the action that mattered may have closed.

This benchmark puts both costs on the same axis. Each approach plans with the
twin it would actually have, is restricted to the decision epochs it would
actually still have available, and both schedules are then scored against the
SAME truth. The difference is value-of-information minus cost-of-delay.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import statistics
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for p in ("services/twin", "services/planner", "packages/fa_core"):
    sys.path.insert(0, str(ROOT / p))

from fa_planner.scheduler import (Economics, ScenarioEvaluator, beam_search,  # noqa: E402
                                  decision_epochs, greedy)
from fa_twin.engine import ZoneConfig  # noqa: E402
from fa_twin.weather import make_synthetic_weather  # noqa: E402

LAT, LON = 30.90, 75.85
ENGINE = "Wofost81_NWLP_CWB_CNB"
CFG = ZoneConfig(zone_id="cmp", latitude=LAT, longitude=LON, crop="rice",
                 variety="Rice_501", sowing_date=dt.date(2025, 6, 20),
                 harvest_date=dt.date(2025, 10, 25), WAV=35.0,
                 NAVAILI=5.0, NSOILBASE=2.0)

FIRST_DECISION = dt.date(2025, 7, 5)     # earliest useful intervention
DELAY_DAYS = 21                          # cost of the observe-first campaign


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenarios", type=int, default=5)
    ap.add_argument("--truth-draws", type=int, default=6)
    ap.add_argument("--epochs", type=int, default=5)
    ap.add_argument("--spread-uninformed", type=float, default=0.22,
                    help="parameter spread the twin carries after ONE capture")
    ap.add_argument("--spread-informed", type=float, default=0.12,
                    help="spread after a 3-week campaign constrains the rates")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "bench" / "results" / "act_vs_observe.json")
    a = ap.parse_args()

    planning_weather = [make_synthetic_weather(LAT, LON, dt.date(2025, 6, 1), days=200, seed=s)
                        for s in range(a.scenarios)]
    # Truth draws are weather the planner never saw -- otherwise the comparison
    # rewards whichever approach happens to overfit the planning ensemble.
    truth_weather = [make_synthetic_weather(LAT, LON, dt.date(2025, 6, 1), days=200,
                                            seed=1000 + s)
                     for s in range(a.truth_draws)]
    econ = Economics()
    all_epochs = decision_epochs(FIRST_DECISION, dt.date(2025, 9, 5), 14)[: a.epochs]
    late_epochs = [e for e in all_epochs if e >= FIRST_DECISION + dt.timedelta(days=DELAY_DAYS)]

    print(f"decision epochs available -- A (act now): {len(all_epochs)}"
          f"  |  B (observe {DELAY_DAYS} d first): {len(late_epochs)}")
    print(f"parameter spread -- A: {a.spread_uninformed:.2f}  B: {a.spread_informed:.2f}\n")

    def plan_with(spread, epochs, label):
        ev = ScenarioEvaluator(CFG, planning_weather, econ, param_spread=spread,
                               seed=0, engine=ENGINE)
        sched = beam_search(ev, epochs, beam_width=4, alpha=0.8, max_applications=2)[0]
        acts = [f"{d:%b%d}:{v.amount:g}" for d, v in sorted(sched.actions.items())
                if v.kind != "none"] or ["none"]
        print(f"  {label:26s} plans {acts}  ({ev.n_rollouts} rollouts)")
        return sched

    sched_a = plan_with(a.spread_uninformed, all_epochs, "A: capture once, act now")
    sched_b = plan_with(a.spread_informed, late_epochs, "B: observe 3 weeks first")

    # Score both against the same unseen truth weather.
    truth_ev = ScenarioEvaluator(CFG, truth_weather, econ, param_spread=0.18,
                                 seed=99, engine=ENGINE)
    res = {}
    for label, sched in (("A_act_now", sched_a), ("B_observe_first", sched_b)):
        scored = truth_ev.score(sched.actions, alpha=0.8)
        res[label] = dict(cvar=scored.cvar, mean=scored.expected_value,
                          yield_kg=scored.mean_yield, cost=scored.cost,
                          plan=[f"{d:%Y-%m-%d}:{v.amount:g}"
                                for d, v in sorted(sched.actions.items()) if v.kind != "none"])
    do_nothing = truth_ev.score({e: sched_a.actions.get(e) for e in []}, alpha=0.8)
    res["do_nothing"] = dict(cvar=do_nothing.cvar, mean=do_nothing.expected_value,
                             yield_kg=do_nothing.mean_yield, cost=0.0, plan=["none"])

    print(f"\n{'approach':<22s} {'CVaR80 (INR/ha)':>17s} {'mean':>10s} {'yield':>8s} {'plan'}")
    for k in ("do_nothing", "A_act_now", "B_observe_first"):
        r = res[k]
        print(f"{k:<22s} {r['cvar']:17.0f} {r['mean']:10.0f} {r['yield_kg']:8.0f}  {r['plan']}")

    delta = res["B_observe_first"]["cvar"] - res["A_act_now"]["cvar"]
    print(f"\nB minus A: {delta:+.0f} INR/ha on CVaR80")
    if delta > 0:
        print("  -> the better-informed decision outweighs the three-week delay")
    elif delta < 0:
        print("  -> the delay costs more than the extra information is worth")
    else:
        print("  -> the two are indistinguishable on this scenario")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"config": {k: str(v) for k, v in vars(a).items()},
                                 "results": res, "B_minus_A_cvar": delta}, indent=2), encoding="utf-8")
    print(f"\nwritten to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
