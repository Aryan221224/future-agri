"""Decisions on the Mead rainfed pivot (US-Ne3): the software's twin policy against standard
practice, season by season, on the real field with the real weather of each year.

Truth here is the simulator (WOFOST anchored to the field's own Sentinel-2 that season, plus
the weed model), because Mead has no measured weed or disease data: this part estimates
decision value; it is not validated against the field. Policies (fa_runner):

  none      no spraying
  calendar  standard practice: fungicide from 30 days after sowing every 10 days, blanket herbicide
  twin      the software: survey scan, twin-planned herbicide (spot / blanket / none), the
            twin's weekly fungicide decision

    python ml/mead/decisions.py --years 2017-2023 --seeds 2
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in ("packages/fa_core", "services/twin", "services/farm", "services/scan", "services/drone",
          "services/runner", "services/planner", "services/loop"):
    sys.path.insert(0, str(ROOT / p))
sys.path.insert(0, str(Path(__file__).parent))
warnings.filterwarnings("ignore")

import study as S  # noqa: E402

# The rainfed pivot as it was farmed each season (AmeriFlux BADM US-Ne3 planting and harvest records):
# no-till maize after soybean, ~113 CRM hybrids, seeding rate in seeds/acre -> seeds/ha, combine yield
# in bu/acre at 15.5 % -> t/ha (x 0.06277). 2023 is not in the records: the 2017-2021 mean is used.
NE3_SEASONS = {
    2017: {"sowing": "05-08", "seeds_ha": 28500 / 0.404686, "hybrid": "Pioneer P1498AM", "measured_t_ha": 192 * 0.06277},
    2019: {"sowing": "04-24", "seeds_ha": 27736 / 0.404686, "hybrid": "Pioneer P1366AM", "measured_t_ha": None},
    2021: {"sowing": "04-29", "seeds_ha": 28000 / 0.404686, "hybrid": "Dekalb DK63-57", "measured_t_ha": 189.5 * 0.06277},
    2023: {"sowing": "05-01", "seeds_ha": 28079 / 0.404686, "hybrid": "assumed", "measured_t_ha": None},
}


def ne3_management(y: int) -> tuple[dict, dict]:
    s = NE3_SEASONS[y]
    return ({"population": round(s["seeds_ha"]), "tillage": "no-till", "previous_crop": "soybean",
             "gdu_black_layer": 2750},
            {"variety": "US_CornBelt_2750GDU", "sowing": s["sowing"], "harvest": "10-15"})

FIELDS = ("yield_dry_kg_ha", "net_ha", "fungicide_sprays", "herbicide", "herbicide_treated_frac", "missions",
          "litres", "drone_time_min", "weed_loss_pct", "scans")


def main(argv=None) -> int:
    from fa_farm.build import build_virtual_farm
    from fa_farm.sources.base import Cache
    from fa_runner.season import run_season
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2017-2023")
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--policies", default="none,calendar,twin")
    a = ap.parse_args(argv)
    lo, _, hi = a.years.partition("-")
    years = [y for y in range(int(lo), int(hi or lo) + 1) if y in NE3_SEASONS]   # Ne3: maize in odd years
    lat, lon, _ = S.SITES["Ne3"]
    poly = S.pivot_polygon(lat, lon)
    cache = Cache(S.DATA / "cache")
    rows, out = [], S.OUT / "decisions.json"
    S.OUT.mkdir(parents=True, exist_ok=True)
    for y in years:
        for seed in range(a.seeds):
            t0 = time.time()
            mg, fld = ne3_management(y)
            vf = build_virtual_farm("A4", y, cache=cache, polygon_wgs84=poly, cell_m=10.0, seed=seed,
                                    management=mg, archetype_overrides=fld)
            for pol in a.policies.split(","):
                r = run_season(vf, pol, seed=seed)
                row = {"year": y, "seed": seed, "policy": pol, "currency": r.currency,
                       "measured_combine_t_ha": NE3_SEASONS[y]["measured_t_ha"], "management": mg,
                       "science_notes": vf.spec.science_notes, "warnings": vf.warnings,
                       **{k: getattr(r, k) for k in FIELDS}, "costs": r.costs_ha}
                rows.append(row)
                print(y, seed, pol, f"yield {r.yield_dry_kg_ha / 1000 / 0.845:.2f} t/ha net {r.net_ha:.0f} "
                      f"{r.currency} fung {r.fungicide_sprays} herb {r.herbicide} ({r.herbicide_treated_frac:.2f}) "
                      f"litres {r.litres:.1f} weed loss {r.weed_loss_pct:.2f}%", flush=True)
                out.write_text(json.dumps(rows, default=str, indent=1))
            print(f"  {y} seed {seed}: {time.time() - t0:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
