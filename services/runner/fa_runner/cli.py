"""Command line for the iteration runner.

    python -m fa_runner farm     --archetype A5 --year 2025
    python -m fa_runner fly      --archetype A5 --year 2025 --height 2.5 --speed 5 --swath 4 --rate 15
    python -m fa_runner season   --archetype A5 --year 2025 --policy twin --seed 0
    python -m fa_runner optimise --archetype A5 --year 2025
    python -m fa_runner perfect  --archetype A5 --year 2025 --seeds 3 --out runs/A5_2025
    python -m fa_runner kml      --archetype A5 --year 2025 --out field.kml
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path


def _farm(a):
    from fa_farm.build import build_virtual_farm
    from fa_farm.sources.base import Cache
    return build_virtual_farm(a.archetype, a.year, Cache(a.cache) if a.cache else Cache(),
                              site_index=a.site, cell_m=a.cell, seed=a.farm_seed)


def _settings(a):
    from fa_drone.mission import MissionSettings
    return MissionSettings(height_m=a.height, speed_ms=a.speed, swath_m=a.swath, rate_l_ha=a.rate,
                           rtk=a.rtk, pump_lpm=a.pump)


def _dump(obj, out: str | None):
    from .report import _default
    txt = json.dumps(obj, default=_default, indent=1)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(txt)
        print(f"wrote {out}")
    else:
        print(txt)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="fa_runner", description="Future Agri iteration runner: virtual "
                                "farm from real data, camera-drone scan, twin, spray-drone twin, perfect run.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--archetype", required=True, help="A1..A8 (see fa_farm/data/archetypes.yaml)")
        sp.add_argument("--year", type=int, required=True, help="season start year")
        sp.add_argument("--site", type=int, default=0)
        sp.add_argument("--cell", type=float, default=10.0, help="grid cell size, m")
        sp.add_argument("--farm-seed", type=int, default=0)
        sp.add_argument("--cache", default=None)
        sp.add_argument("--out", default=None)

    def flight(sp):
        sp.add_argument("--height", type=float, default=2.5)
        sp.add_argument("--speed", type=float, default=5.0)
        sp.add_argument("--swath", type=float, default=4.0)
        sp.add_argument("--rate", type=float, default=15.0)
        sp.add_argument("--pump", type=float, default=3.5)
        sp.add_argument("--rtk", action="store_true")

    s = sub.add_parser("farm", help="build the virtual test farm from real data")
    common(s)
    s = sub.add_parser("fly", help="simulate one blanket spray mission")
    common(s)
    flight(s)
    s.add_argument("--wind", type=float, default=2.0)
    s.add_argument("--wind-dir", type=float, default=270.0)
    s.add_argument("--gust", type=float, default=3.5)
    s.add_argument("--temp", type=float, default=20.0)
    s = sub.add_parser("season", help="one closed-loop season")
    common(s)
    flight(s)
    s.add_argument("--policy", default="twin", choices=["none", "calendar", "twin", "twin_alert"])
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--members", type=int, default=10)
    s = sub.add_parser("optimise", help="rank flight settings for this drone on this field")
    common(s)
    s = sub.add_parser("perfect", help="search for the perfect run (flight + season plan)")
    common(s)
    s.add_argument("--seeds", type=int, default=3)
    s.add_argument("--lam", type=float, default=0.5)
    s.add_argument("--workers", type=int, default=None)
    s.add_argument("--members", type=int, default=10)
    s = sub.add_parser("kml", help="export the field polygon for JIYI Agri Assistant")
    common(s)

    a = p.parse_args(argv)
    vf = _farm(a)
    if a.cmd == "farm":
        _dump(vf.summary(), a.out)
    elif a.cmd == "fly":
        from fa_drone.mission import Weather, simulate_mission
        from fa_drone.spec import load_spec
        from fa_farm.grid import FarmGrid
        from fa_farm.regions import load_region
        g = FarmGrid.from_wgs84(vf.spec.polygon_wgs84, a.cell)
        r = simulate_mission(g, load_spec(), _settings(a), Weather(a.wind, a.wind_dir, a.gust, a.temp),
                             rules=load_region(vf.spec.archetype.region).rules)
        _dump({"feasible": r.feasible, "reasons": r.reasons, "kpis": r.kpis, "warnings": r.warnings,
               "telemetry": r.telemetry}, a.out)
    elif a.cmd == "season":
        from .season import run_season
        r = run_season(vf, a.policy, seed=a.seed, flight=_settings(a), plan_members=a.members)
        _dump(r, a.out)
    elif a.cmd == "optimise":
        from fa_drone.spec import load_spec
        from fa_farm.regions import load_region
        from .economics import Economics
        from .optimise import optimise_flight
        from .perfect import _patch, prior_yield
        from .scenarios import spray_window, wind_samples
        from fa_farm.archetypes import season_dates
        spec, region = load_spec(), load_region(vf.spec.archetype.region)
        econ = Economics.from_region(region, vf.spec.archetype.crop)
        sow, harv = season_dates(vf.spec.archetype, a.year)
        months = {sow.month, harv.month} | {m for m in range(1, 13) if sow.month < m < harv.month}
        winds = wind_samples(vf.history, months, spray_window(region, spec), 6) or [(1.0, 270.0, 1.6, 20.0)]
        var = 0.10 * econ.revenue(max(prior_yield(vf), 1000.0))
        ranked = optimise_flight(_patch(vf), spec, region, econ, winds, var)
        _dump([dataclasses.asdict(r) for r in ranked], a.out)
    elif a.cmd == "perfect":
        from .perfect import perfect_run
        from .report import write_report
        pr = perfect_run(vf, seeds=tuple(range(a.seeds)), lam=a.lam, workers=a.workers,
                         plan_members=a.members, cache_root=a.cache)
        out = a.out or f"runs/{a.archetype}_{a.year}"
        from fa_drone.mission import MissionSettings
        paths = write_report({"perfect_run": pr, **{k: v for k, v in pr.seasons.items()}}, vf, out,
                             settings=MissionSettings(**pr.best["jiyi_settings"]))
        print(json.dumps({"best": pr.best, "ranked": pr.ranked, "files": paths}, indent=1, default=str))
    elif a.cmd == "kml":
        from fa_drone.jiyi_io import export_kml
        print(export_kml(vf.spec.polygon_wgs84, [], f"{a.archetype} {vf.spec.site.name}",
                         a.out or "field_for_jiyi.kml"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
