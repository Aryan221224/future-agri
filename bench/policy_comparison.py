"""Closed-loop policy comparison: what the whole twin is worth, season by season.

Each season runs the same truth (weather, blight arrival, weed field, soil)
under every policy, so differences are *paired*. Truth = WOFOST coupled to a
late-blight epidemic at Bennett (potato, 25 Oct - 10 Feb) on normals-calibrated
weather; the twin's soil parameters are wrong by up to +-10 %. Policies are
documented in ``fa_loop.season``.

Reported with 95 % bootstrap intervals over seasons: water, sprays, flights,
yield and net Rs/ha, and the twin-minus-farmer differences. All economics are
stated assumptions (``PotatoEconomics``); the blight model infects ~65 % of
seasons. This is a simulation, not a field result.

    python bench/policy_comparison.py --seasons 40 --workers 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import warnings
from multiprocessing import Pool
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/twin", "services/planner", "services/loop"):
    sys.path.insert(0, str(ROOT / p))
warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

from fa_loop.season import POLICIES, simulate_season  # noqa: E402

FIELDS = ("fresh_t_ha_farm", "water_cm", "irrigations", "sprays", "flights", "net_rs_ha",
          "final_severity", "weed_loss_pct")


def one(args):
    seed, members = args
    out = {}
    for pol in POLICIES:
        r = simulate_season(seed, pol, plan_members=members)
        out[pol] = {k: getattr(r, k) for k in FIELDS} | {"infected": r.infected,
                                                          "herbicide": r.herbicide}
    return seed, out


def ci(x, n_boot=4000, seed=0):
    x = np.asarray(x, float)
    rng = np.random.default_rng(seed)
    b = rng.choice(x, (n_boot, len(x))).mean(1)
    return [round(float(np.percentile(b, 2.5)), 2), round(float(np.percentile(b, 97.5)), 2)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, default=40)
    ap.add_argument("--members", type=int, default=10)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / "policy_comparison.json"))
    a = ap.parse_args()
    t0 = time.time()
    with Pool(a.workers) as pool:
        rows = dict(pool.map(one, [(s, a.members) for s in range(a.seasons)]))
    seeds = sorted(rows)
    S = {"setup": dict(site="Bennett 28.45N 77.58E", crop="potato Potato_701, 25 Oct - 10 Feb",
                       seasons=a.seasons, plan_members=a.members,
                       economics="potato Rs10/kg fresh (farm = 0.5 x model ceiling), fungicide Rs1500/spray, "
                                 "scouting Rs300/flight, herbicide Rs2000 blanket, water Rs300/cm",
                       note="simulated; blight/weed/price figures are stated assumptions"),
         "seasons_with_blight": int(sum(rows[s]["farmer"]["infected"] for s in seeds))}
    for pol in POLICIES:
        S[pol] = {k: round(float(np.mean([rows[s][pol][k] for s in seeds])), 2) for k in FIELDS}
        S[pol]["net_ci95"] = ci([rows[s][pol]["net_rs_ha"] for s in seeds])
    for pol in POLICIES:
        if pol == "farmer":
            continue
        d = {k: [rows[s][pol][k] - rows[s]["farmer"][k] for s in seeds]
             for k in ("water_cm", "sprays", "fresh_t_ha_farm", "net_rs_ha")}
        S[f"{pol}_minus_farmer"] = {
            k: {"mean": round(float(np.mean(v)), 2), "ci95": ci(v),
                "better_in": f"{int(sum((x < 0) if k in ('water_cm', 'sprays') else (x > 0) for x in v))}/{len(v)}"}
            for k, v in d.items()}
        S[f"{pol}_minus_farmer"]["water_pct"] = round(
            100 * float(np.sum(d["water_cm"])) / max(1e-9, float(np.sum([rows[s]["farmer"]["water_cm"] for s in seeds]))), 1)
    S["runtime_s"] = round(time.time() - t0, 1)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump({"summary": S, "seasons": rows}, open(a.out, "w"), indent=1, default=str)
    print(json.dumps(S, indent=1))
    print("->", a.out)


if __name__ == "__main__":
    main()
