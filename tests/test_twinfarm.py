"""Task 10: global economics and the twin running on the DigitalFarm only."""
import datetime as dt

import numpy as np
import pytest

from fa_farm.archetypes import load_archetypes
from fa_farm.regions import load_region
from fa_farm.truth import GroundTruthFarm
from fa_runner.economics import Economics
from fa_runner.twinfarm import TwinFarm
from fa_scan.digital import build_digital_farm
from fa_scan.measure import simulate_scan
from fa_scan.survey import plan_scan
from fa_twin.engine import SoilParams
from test_truth import NL_NORMALS, _spec, _weather


def test_economics_from_region_eu_potato():
    e = Economics.from_region(load_region("EU"), "potato")
    assert e.currency == "EUR" and e.price_per_kg == pytest.approx(0.15) and e.dry_matter == 0.20
    pe = e.planner_econ()
    assert pe.fungicide_per_spray == pytest.approx(40.0 + 35.0)
    assert pe.price_per_kg_fresh == pytest.approx(0.15) and pe.farm_factor == 1.0
    assert e.revenue(1000.0) == pytest.approx(0.15 * 1000 / 0.20)


def test_economics_missing_price_is_explicit():
    with pytest.raises(KeyError):
        Economics.from_region(load_region("KE"), "potato")


def test_twin_runs_on_digital_farm_only():
    a = load_archetypes()["A5"]
    wx = _weather(dt.date(2025, 1, 1), 365, NL_NORMALS)
    truth = GroundTruthFarm(_spec(w=120, h=100, cell=5.0, inoculum=False, seed=3), wx)
    day = dt.date(2025, 6, 4)
    truth.advance_to(day)
    obs = simulate_scan(truth.state(), truth.grid, plan_scan(truth.grid, "p4rtk", 0.005),
                        np.random.default_rng(0), crop="potato", days_after_sowing=45)
    df = build_digital_farm(obs, truth.grid)
    lat, lon = truth.spec.centroid
    tw = TwinFarm(a, load_region("EU"), lat, lon, [r for r in wx if r["DAY"] < day],
                  SoilParams(), truth.sowing, truth.harvest_date)
    d = tw.ingest(df, day - dt.timedelta(days=1))
    assert d.kind == "pass"
    assert tw.herbicide_option in ("none", "blanket", "spot")
    targets = tw.weed_targets()
    assert targets.shape == (truth.grid.n_cells,) and targets.dtype == bool


def test_twin_advises_herbicide_for_non_potato_crops():
    """Global product: weed economics exist for every crop with a regional price."""
    a = load_archetypes()["A6"]
    wx = _weather(dt.date(2025, 1, 1), 365, NL_NORMALS)
    spec = _spec(arch="A6", w=60, h=60, cell=5.0, inoculum=False, seed=2)
    truth = GroundTruthFarm(spec, wx)
    day = truth.sowing + dt.timedelta(days=30)
    truth.advance_to(day)
    obs = simulate_scan(truth.state(), truth.grid, plan_scan(truth.grid, "p4rtk", 0.005),
                        np.random.default_rng(0), crop="maize", days_after_sowing=30)
    df = build_digital_farm(obs, truth.grid)
    lat, lon = truth.spec.centroid
    tw = TwinFarm(a, load_region("KE"), lat, lon, [r for r in wx if r["DAY"] < day], SoilParams(),
                  truth.sowing, truth.harvest_date, expected_yield_kg_ha=6000.0)
    tw.ingest(df, day - dt.timedelta(days=1))
    assert tw.herbicide_option in ("blanket", "spot")
