"""Task 5: the ground-truth farm (the 'real farm' the twin never sees)."""
import datetime as dt

import numpy as np
import pytest
from shapely.geometry import box

from fa_farm.archetypes import load_archetypes
from fa_farm.grid import FarmGrid
from fa_farm.heterogeneity import gaussian_field, soil_zones, weed_seedbank
from fa_farm.truth import AsApplied, FarmSpec, GroundTruthFarm
from fa_twin.engine import SoilParams
from fa_twin.weather import generate_records

# Temperate maritime test normals (Tmax, Tmin, rain mm, rain days, MJ/m2/d, VAP hPa, wind m/s)
NL_NORMALS = {1: (6, 1, 70, 12, 2.5, 7, 5), 2: (7, 1, 55, 10, 5, 7, 5), 3: (10, 3, 60, 11, 9, 8, 5),
              4: (14, 5, 45, 9, 14, 9, 4.5), 5: (18, 8, 55, 9, 18, 12, 4), 6: (21, 11, 65, 9, 19, 14, 4),
              7: (23, 13, 75, 9, 19, 16, 4), 8: (23, 13, 80, 10, 16, 16, 4), 9: (19, 11, 80, 11, 11, 14, 4.5),
              10: (15, 8, 85, 12, 6, 12, 5), 11: (10, 4, 85, 13, 3, 9, 5), 12: (7, 2, 80, 13, 2, 8, 5)}
TROP_NORMALS = {m: (32, 21, 200 if m in (10, 11, 12, 1, 2, 3) else 30,
                    15 if m in (10, 11, 12, 1, 2, 3) else 3, 19, 24, 2) for m in range(1, 13)}


def _poly(w_m=200, h_m=100, lat=52.52, lon=5.62):
    dlat = h_m / 111_320
    dlon = w_m / (111_320 * np.cos(np.radians(lat)))
    return box(lon, lat, lon + dlon, lat + dlat)


def _weather(start, days, normals, seed=3):
    return generate_records(start, days, seed=seed, normals=normals)


def _spec(arch="A5", year=2025, w=100, h=100, cell=10.0, inoculum=None, seed=1, normals=NL_NORMALS):
    a = load_archetypes()[arch]
    return FarmSpec(archetype=a, site=a.locations[0], year=year, polygon_wgs84=_poly(w, h),
                    soil=SoilParams(), elevation_m=0.0, region=a.region, cell_m=cell, seed=seed,
                    weed_mean_m2=8.0, blight_inoculum=inoculum)


def test_grid_area_matches_polygon():
    g = FarmGrid.from_wgs84(_poly(200, 100), cell_m=5.0)
    assert abs(g.area_ha - 2.0) / 2.0 < 0.02
    assert g.n_cells == int(round(g.area_ha * 1e4 / 25))
    left = g.cells_in(box(*g.bounds_local[:2], g.bounds_local[0] + 100, g.bounds_local[3]))
    assert 0.4 < left.mean() < 0.6


def test_gaussian_field_standardised_and_smooth():
    f = gaussian_field((60, 60), cell_m=5.0, corr_m=40.0, rng=np.random.default_rng(0))
    assert abs(f.mean()) < 0.05 and abs(f.std() - 1) < 0.05
    assert np.corrcoef(f[:, :-1].ravel(), f[:, 1:].ravel())[0, 1] > 0.8


def test_zones_partition_cells():
    g = FarmGrid.from_wgs84(_poly(200, 100), cell_m=5.0)
    z, soils = soil_zones(g, SoilParams(), twi_cells=None, rng=np.random.default_rng(1), k=4)
    assert z.shape == (g.n_cells,) and set(np.unique(z)) <= {0, 1, 2, 3}
    for s in soils:
        assert s.SMW < s.SMFCF < s.SM0
    assert len({round(s.SMFCF, 4) for s in soils}) > 1


def test_weed_clusters_patchy():
    g = FarmGrid.from_wgs84(_poly(300, 300), cell_m=5.0)
    d = weed_seedbank(g, np.random.default_rng(2), mean_m2=10.0, clusters_per_ha=3.0, radius_m=12.0)
    assert abs(d.mean() - 10.0) / 10.0 < 0.3
    assert d.var() / d.mean() > 2.0


def test_truth_season_runs_and_cells_vary():
    spec = _spec(inoculum=False)
    wx = _weather(dt.date(2025, 1, 1), 365, NL_NORMALS)
    t = GroundTruthFarm(spec, wx)
    st = t.state(dt.date(2025, 6, 15))
    assert st.lai.shape == (t.grid.n_cells,) and st.lai.mean() > 0.3
    assert np.all((st.cc >= 0) & (st.cc <= 1))
    h = t.harvest()
    assert h.field_yield_kg_ha > 1000
    assert h.yield_kg_ha_cells.std() > 0


def test_herbicide_dose_reduces_weeds_only_where_applied():
    spec = _spec(inoculum=False)
    t = GroundTruthFarm(spec, _weather(dt.date(2025, 1, 1), 365, NL_NORMALS))
    day = dt.date(2025, 5, 20)
    before = t.state(day).weeds_m2.copy()
    dose = np.zeros(t.grid.n_cells)
    half = t.grid.cells_xy[:, 0] < np.median(t.grid.cells_xy[:, 0])
    dose[half] = 1.0
    t.apply(AsApplied(day=day, product="herbicide", dose_frac=dose, off_target_l=0.0))
    after = t.state(day).weeds_m2
    assert after[half].sum() < 0.2 * before[half].sum()
    np.testing.assert_allclose(after[~half], before[~half])


def test_underdosed_strip_keeps_blight():
    spec = _spec(inoculum=True, seed=4)
    t = GroundTruthFarm(spec, _weather(dt.date(2025, 1, 1), 365, NL_NORMALS, seed=11))
    assert t.blight_infection_day is not None
    d0 = t.blight_infection_day - dt.timedelta(days=1)
    strip = t.grid.cells_xy[:, 1] < np.quantile(t.grid.cells_xy[:, 1], 0.2)
    dose = np.where(strip, 0.0, 1.0)
    day = d0
    for _ in range(6):                      # weekly protectant, missing the strip
        t.apply(AsApplied(day=day, product="fungicide", dose_frac=dose, off_target_l=0.0))
        day += dt.timedelta(days=6)
        t.advance_to(day)
    s = t.state(day).blight_sev
    assert s[strip].mean() > s[~strip].mean()


def test_southern_hemisphere_season_runs():
    """Review focus 3: Oct -> Feb season crosses New Year without breaking."""
    a = load_archetypes()["A3"]
    spec = FarmSpec(archetype=a, site=a.locations[0], year=2024, polygon_wgs84=_poly(100, 100, -12.5, -55.7),
                    soil=SoilParams(), elevation_m=380.0, region="BR", cell_m=10.0, seed=2,
                    weed_mean_m2=5.0, blight_inoculum=False)
    t = GroundTruthFarm(spec, _weather(dt.date(2024, 9, 1), 240, TROP_NORMALS))
    assert t.sowing == dt.date(2024, 10, 1) and t.harvest_date.year == 2025
    assert t.harvest().field_yield_kg_ha > 0


def test_anchor_moves_toward_s2():
    from fa_farm.anchor import anchor_to_cover, simulate_cover
    from fa_twin.engine import ZoneConfig
    a = load_archetypes()["A5"]
    wx = _weather(dt.date(2025, 1, 1), 365, NL_NORMALS, seed=5)
    cfg = ZoneConfig("z", 52.52, 5.62, a.crop, a.variety, dt.date(2025, 4, 20), dt.date(2025, 9, 15))
    true_tsum2 = 2100 * 0.80
    days = [dt.date(2025, 5, 25) + dt.timedelta(days=10 * i) for i in range(10)]
    cover = simulate_cover(cfg, wx, days, {"TSUM2": true_tsum2})
    ser = [(d, None, c) for d, c in zip(days, cover)]
    fit, info = anchor_to_cover(cfg, wx, ser, members=16, seed=0)
    assert abs(fit["TSUM2"] - true_tsum2) < abs(2100 - true_tsum2)
    assert info["n_obs"] == 10


def test_dose_efficacy_shape():
    from fa_twin.damage import dose_efficacy
    assert dose_efficacy(0.0, 0.85) == 0.0
    assert abs(float(dose_efficacy(1.0, 0.85)) - 0.85) < 1e-9
    e = dose_efficacy(np.linspace(0, 2, 21), 0.9)
    assert np.all(np.diff(e) >= 0) and e.max() <= 1.0


@pytest.mark.network
def test_build_virtual_farm_flevoland_network(tmp_path):
    from fa_farm.build import build_virtual_farm
    from fa_farm.sources.base import Cache
    vf = build_virtual_farm("A5", 2025, Cache(tmp_path), cell_m=10.0)
    s = vf.summary()
    assert 5 <= s["area_ha"] <= 21 and s["s2_observations"] >= 3
    assert {p["source"].split()[0] for p in s["provenance"]} >= {"Fields", "SoilGrids", "NASA"}
    h = vf.truth().harvest()
    assert h.field_yield_kg_ha > 1000
