"""Task 11: future iterations, flight optimisation, closed-loop season, perfect run, reports."""
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import box

from fa_drone.mission import MissionSettings
from fa_drone.spec import load_spec
from fa_farm.archetypes import load_archetypes
from fa_farm.build import VirtualFarm
from fa_farm.grid import FarmGrid
from fa_farm.regions import load_region
from fa_farm.sources.forecasts import ForecastSource
from fa_farm.truth import FarmSpec
from fa_runner.economics import Economics
from fa_runner.optimise import optimise_flight
from fa_runner.report import write_report
from fa_runner.scenarios import ForecastFeed
from fa_runner.season import run_season
from fa_twin.engine import SoilParams
from test_truth import NL_NORMALS, _weather

ROOT = Path(__file__).resolve().parents[1]
SPEC = load_spec()


def _vf(w=100, h=100, inoculum=False, windy=False, seed=1):
    a = load_archetypes()["A5"]
    lat, lon = 52.52, 5.62
    dlat, dlon = h / 111_320, w / (111_320 * np.cos(np.radians(lat)))
    hist = []
    for y in range(2015, 2025):
        hist += _weather(dt.date(y, 1, 1), 365 + (y % 4 == 0), NL_NORMALS, seed=y)
    season = _weather(dt.date(2025, 2, 1), 260, NL_NORMALS, seed=99)
    if windy:
        season = [dict(r, WIND=9.0) for r in season]
    spec = FarmSpec(archetype=a, site=a.locations[0], year=2025,
                    polygon_wgs84=box(lon, lat, lon + dlon, lat + dlat), soil=SoilParams(),
                    elevation_m=0.0, region="EU", cell_m=10.0, seed=seed, weed_mean_m2=6.0,
                    blight_inoculum=inoculum)
    return VirtualFarm(spec, season, hist, [], {}, [], [])


def _feed(vf):
    lat, lon = vf.spec.centroid
    return ForecastFeed(lat, lon, vf.history, sources=[ForecastSource.CLIMATOLOGY])


def test_feed_members_span_observed_to_harvest():
    vf = _vf()
    feed = _feed(vf)
    obs = [r for r in vf.season_weather if r["DAY"] < dt.date(2025, 6, 1)]
    mem = feed.members(dt.date(2025, 6, 1), obs, dt.date(2025, 9, 15), n=4, seed=0)
    assert len(mem) == 4
    assert mem[0][0]["DAY"] == obs[0]["DAY"] and mem[0][-1]["DAY"] == dt.date(2025, 9, 15)
    assert feed.sources_used[ForecastSource.CLIMATOLOGY.value] == 1


def test_flight_optimiser_ranks_only_feasible():
    vf = _vf(w=100, h=60)
    grid = FarmGrid.from_wgs84(vf.spec.polygon_wgs84, 5.0)
    econ = Economics.from_region(load_region("EU"), "potato")
    winds = [(1.5, 270.0, 3.0, 18.0), (2.5, 200.0, 4.0, 20.0), (9.0, 270.0, 12.0, 18.0)]
    ranked = optimise_flight(grid, SPEC, load_region("EU"), econ, winds, value_at_risk_ha=300.0,
                             heights=(2.0, 3.0), speeds=(4.0, 6.0), swaths=(3.5, 5.0),
                             rates=(10.0, 15.0), rtk=(False, True), top=4, seed=0)
    assert ranked and all(r.feasible_frac > 0 for r in ranked)
    best = ranked[0]
    assert best.settings.speed_ms <= SPEC.max_work_speed_ms
    assert best.score >= ranked[-1].score
    assert {"cv_pct", "skipped_frac", "time_min", "litres"} <= set(best.kpis_mean)


def test_no_window_when_always_windy():
    """Review focus 4: wind above the limit every day -> no missions flown."""
    vf = _vf(windy=True)
    r = run_season(vf, "calendar", seed=0, feed=_feed(vf), spec=SPEC)
    assert r.missions == 0 and r.litres == 0
    assert any("no feasible spray window" in n for n in r.notes)


def test_twin_uses_less_than_calendar_without_disease():
    vf = _vf(inoculum=False)
    feed = _feed(vf)
    twin = run_season(vf, "twin", seed=0, feed=feed, spec=SPEC, plan_members=6)
    cal = run_season(vf, "calendar", seed=0, feed=_feed(vf), spec=SPEC)
    assert twin.fungicide_sprays < cal.fungicide_sprays
    assert twin.litres < cal.litres
    assert twin.yield_dry_kg_ha > 0 and cal.yield_dry_kg_ha > 0
    assert twin.scans >= 1 and "pass" in {d["kind"] for d in twin.decisions}


def test_report_files_and_attribution(tmp_path):
    vf = _vf()
    r = run_season(vf, "none", seed=0, feed=_feed(vf), spec=SPEC)
    paths = write_report({"season": r}, vf, tmp_path)
    for k in ("json", "csv", "geojson", "kml"):
        assert Path(paths[k]).exists()
    js = json.loads(Path(paths["json"]).read_text())
    assert "attribution" in js and "warnings" in js


def test_cli_help():
    out = subprocess.run([sys.executable, "-m", "fa_runner", "--help"], capture_output=True, text=True,
                         cwd=ROOT, env={"PYTHONPATH": ":".join(str(ROOT / p) for p in (
                             "packages/fa_core", "services/twin", "services/planner", "services/loop",
                             "services/farm", "services/scan", "services/drone", "services/runner")),
                             "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0 and "perfect" in out.stdout


def test_flight_weather_uses_calmer_dawn_wind():
    from fa_runner.scenarios import DAWN_WIND_FACTOR, flight_record, mission_weather
    rec = {"DAY": dt.date(2025, 6, 1), "TMAX": 20.0, "TMIN": 10.0, "RAIN": 0.0, "WIND": 4.0}
    fr = flight_record(rec)
    assert fr["WIND"] == pytest.approx(4.0 * DAWN_WIND_FACTOR)
    w = mission_weather(rec, np.random.default_rng(0))
    assert w.wind_ms == pytest.approx(4.0 * DAWN_WIND_FACTOR)


def test_herbicide_waits_longer_than_fungicide():
    from fa_runner.season import MAX_WAIT_DAYS
    assert MAX_WAIT_DAYS["herbicide"] >= 14 and MAX_WAIT_DAYS["fungicide"] <= 7


def test_logistics_sensitivity_reports_packs_and_chargers():
    from fa_runner.optimise import logistics_sensitivity
    vf = _vf(w=150, h=100)
    grid = FarmGrid.from_wgs84(vf.spec.polygon_wgs84, 10.0)
    rows = logistics_sensitivity(grid, SPEC, load_region("EU"), MissionSettings(),
                                 [(1.5, 270.0, 2.4, 18.0)], options=((4, 1), (6, 2)))
    assert [(r["battery_sets"], r["chargers"]) for r in rows] == [(4, 1), (6, 2)]
    assert rows[1]["time_min"] <= rows[0]["time_min"]


def test_perfect_run_end_to_end_fields():
    from fa_runner.perfect import perfect_run
    vf = _vf(w=80, h=60)
    pr = perfect_run(vf, seeds=(0,), workers=1, plan_members=4, n_winds=2,
                     flight_grid=dict(heights=(2.5,), speeds=(5.0, 7.0), swaths=(4.0,), rates=(15.0,),
                                      rtk=(False,)),
                     sources=[ForecastSource.CLIMATOLOGY], progress=lambda m: None)
    assert pr.best["plan"] in {t["plan"] for t in pr.ranked}
    assert pr.logistics and "battery_sets" in pr.logistics[0]
    assert set(pr.timings_s) == {"stage1", "stage2"}
    assert all(isinstance(w, str) for w in pr.warnings)
    assert pr.flight_ranking and pr.flight_ranking[0]["scale"].startswith("whole field")
    assert pr.flight_ranking_patch and pr.flight_ranking_patch[0]["scale"].startswith("patch")
    assert any(t["plan"] == "twin_optimised_early_scan" and t["scan_das"] == 30 for t in pr.ranked)
