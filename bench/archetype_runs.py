"""Perfect-run benchmark across the research archetypes, on real data.

For each archetype: build the virtual farm from real data (FTW boundary,
SoilGrids, Copernicus DEM, NASA POWER, Sentinel-2 anchoring), search flight
settings for the 10 L K3-A Pro drone, run every candidate plan as closed-loop
seasons driven by the real archived ensemble forecasts, and record the ranked
plans and the perfect run.

    python bench/archetype_runs.py --archetypes A5 A6 A4 A3 A1 --seeds 3 --workers 8
    (results appended per archetype to bench/results/archetypes.json)
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/twin", "services/planner", "services/loop", "services/farm",
          "services/scan", "services/drone", "services/runner"):
    sys.path.insert(0, str(ROOT / p))

SEASON_YEAR = {"A1": 2025, "A2": 2025, "A3": 2024, "A4": 2025, "A5": 2025, "A6": 2025}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archetypes", nargs="+", default=["A5", "A6", "A4", "A3", "A1", "A2"])
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--members", type=int, default=10)
    ap.add_argument("--cell", type=float, default=10.0)
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / "archetypes.json")
    a = ap.parse_args()
    from fa_farm.build import build_virtual_farm
    from fa_farm.sources.base import Cache
    from fa_runner.perfect import perfect_run
    from fa_runner.report import ATTRIBUTION, _default, write_report

    res = json.loads(a.out.read_text()) if a.out.exists() else {}
    res["attribution"] = ATTRIBUTION
    res.setdefault("runs", {})
    for aid in a.archetypes:
        t0 = time.time()
        year = SEASON_YEAR.get(aid, 2025)
        print(f"=== {aid} {year}", flush=True)
        try:
            vf = build_virtual_farm(aid, year, Cache(), cell_m=a.cell if aid != "A6" else 5.0)
            pr = perfect_run(vf, seeds=tuple(range(a.seeds)), workers=a.workers, plan_members=a.members)
            srcs: dict = {}
            for rs in pr.seasons.values():
                for r in rs:
                    for k, v in r.forecast_sources.items():
                        srcs[k] = srcs.get(k, 0) + v
            from fa_drone.mission import MissionSettings
            paths = write_report({"perfect_run": pr, **pr.seasons}, vf, ROOT / "runs" / f"{aid}_{year}",
                                 settings=MissionSettings(**pr.best["jiyi_settings"]))
            res["runs"][aid] = {"farm": vf.summary(), "best": pr.best, "ranked": pr.ranked,
                                "flight_top3": pr.flight_ranking[:3], "value_at_risk_ha": pr.value_at_risk_ha,
                                "currency": pr.currency, "forecast_issues_by_source": srcs,
                                "warnings": pr.warnings, "timings_s": pr.timings_s,
                                "minutes": round((time.time() - t0) / 60, 1), "report": paths}
            print(json.dumps({"best": pr.best["plan"], "net": pr.best["net_mean"]}, default=str), flush=True)
        except Exception as e:  # noqa: BLE001 -- one archetype must not sink the batch
            res["runs"][aid] = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]}
            print("FAILED", aid, e, flush=True)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(res, default=_default, indent=1))
    print("ARCHETYPES_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
