"""Task 7: spray pattern, GNSS error, K3-A Pro route behaviour and mission simulation."""
import numpy as np
import pytest
from shapely.geometry import box

from fa_drone.gnss import GnssError
from fa_drone.k3a import generate_route
from fa_drone.mission import MissionSettings, Weather, simulate_mission
from fa_drone.spec import load_spec
from fa_drone.spray import SprayModel
from fa_farm.grid import FarmGrid

SPEC = load_spec("t1_10l")


def _field(w_m, h_m, cell=5.0, lat=52.52, lon=5.62):
    dlat = h_m / 111_320
    dlon = w_m / (111_320 * np.cos(np.radians(lat)))
    return FarmGrid.from_wgs84(box(lon, lat, lon + dlon, lat + dlat), cell_m=cell)


CALM = Weather(wind_ms=1.0, wind_dir_deg=270.0, gust_ms=2.0, temp_c=18.0)


def test_flow_linkage():
    sm = SprayModel(SPEC)
    assert sm.flow_lpm(5.0, 15.0, 4.0, 3.5) == pytest.approx(1.8)
    assert sm.flow_lpm(0.4, 15.0, 4.0, 3.5) == 0.0
    assert sm.flow_lpm(7.0, 25.0, 4.0, 3.5) == pytest.approx(3.5)


def test_effective_swath_reasonable():
    sm = SprayModel(SPEC)
    w = sm.effective_swath(2.5, 0.0)
    assert 3.0 <= w <= 7.0
    assert sm.effective_swath(3.5, 0.0) > sm.effective_swath(1.5, 0.0)


def test_cv_hand_case_dense_passes_uniform():
    sm = SprayModel(SPEC)
    assert sm.pattern_cv(2.5, spacing_m=0.5, wind_cross_ms=0.0) < 0.02


def test_crosswind_worsens_uniformity():
    sm = SprayModel(SPEC)
    assert sm.pattern_cv(2.5, 4.0, 3.0) > sm.pattern_cv(2.5, 4.0, 0.0)
    assert sm.off_target_frac(2.5, 4.0, 5.0) > sm.off_target_frac(2.5, 0.5, 5.0)


def test_gnss_error_scale():
    g = GnssError(rtk=False, seed=0)
    e = np.array([g.step(1.0) for _ in range(5000)])
    r = np.hypot(e[:, 0], e[:, 1])
    assert 1.0 < np.percentile(r, 95) < 3.5          # "+-2 m" class
    gr = GnssError(rtk=True, seed=0)
    er = np.array([gr.step(1.0) for _ in range(2000)])
    assert np.percentile(np.hypot(er[:, 0], er[:, 1]), 95) < 0.1


def test_route_waypoint_groups_respect_k3a_limit():
    g = _field(800, 750, cell=10.0)                    # ~60 ha
    r = generate_route(g, MissionSettings(swath_m=3.0), SPEC)
    assert len(r.groups) >= 2
    assert all(2 * len(grp) <= SPEC.fc.max_waypoints_per_group for grp in r.groups)


def test_rtk_reduces_skips():
    g = _field(200, 100)
    base = MissionSettings(height_m=2.5, speed_ms=5.0, swath_m=4.0, rate_l_ha=15.0)
    no = simulate_mission(g, SPEC, base, CALM, seed=3)
    rtk = simulate_mission(g, SPEC, MissionSettings(**{**base.__dict__, "rtk": True}), CALM, seed=3)
    assert rtk.kpis["cv_pct"] < no.kpis["cv_pct"]
    assert rtk.kpis["skipped_frac"] <= no.kpis["skipped_frac"]


def test_tank_forces_refills():
    g = _field(200, 100)                              # 2 ha x 15 L/ha = 30 L = 3 tank loads
    r = simulate_mission(g, SPEC, MissionSettings(rate_l_ha=15.0), CALM, seed=1)
    assert r.feasible
    assert r.kpis["refills"] >= 2 and r.kpis["sorties"] >= 3
    assert r.kpis["litres"] == pytest.approx(30.0, rel=0.12)


def test_tiny_field_one_pass_or_refusal():
    """Review focus 1: a 0.03 ha strip narrower than a pass."""
    g = _field(100, 3, cell=1.0)
    r = simulate_mission(g, SPEC, MissionSettings(), CALM, seed=0)
    if r.feasible:
        assert r.kpis["sorties"] >= 1 and r.dose_l_ha.max() > 0
    else:
        assert r.reasons


def test_wind_over_limit_infeasible():
    g = _field(100, 100)
    r = simulate_mission(g, SPEC, MissionSettings(), Weather(9.0, 270.0, 12.0, 18.0), seed=0)
    assert not r.feasible and any("wind" in x for x in r.reasons)
    assert r.dose_l_ha.sum() == 0


def test_spot_spray_only_targets():
    g = _field(200, 200)
    xy = g.cells_xy
    targets = np.hypot(xy[:, 0] - xy[:, 0].min() - 50, xy[:, 1] - xy[:, 1].min() - 50) < 25
    spot = simulate_mission(g, SPEC, MissionSettings(), CALM, targets=targets, seed=0)
    full = simulate_mission(g, SPEC, MissionSettings(), CALM, seed=0)
    far = np.hypot(xy[:, 0] - xy[:, 0].min() - 150, xy[:, 1] - xy[:, 1].min() - 150) < 30
    assert spot.dose_l_ha[far].max() == 0
    assert spot.dose_l_ha[targets].mean() > 0.6 * 15.0
    assert spot.kpis["litres"] < 0.35 * full.kpis["litres"]


def test_kpis_complete():
    g = _field(100, 100)
    r = simulate_mission(g, SPEC, MissionSettings(), CALM, seed=0)
    for k in ("time_min", "flight_min", "energy_wh", "wh_per_ha", "litres", "sorties", "battery_swaps",
              "refills", "cv_pct", "skipped_frac", "overdosed_frac", "off_target_frac", "area_ha",
              "ha_per_hour", "waypoints"):
        assert k in r.kpis
    assert 0 <= r.kpis["off_target_frac"] < 0.5


def test_long_field_relocates_home_within_link_range():
    g = _field(1600, 60, cell=10.0)
    r = simulate_mission(g, SPEC, MissionSettings(swath_m=5.0), CALM, seed=0)
    assert r.feasible and r.kpis["home_moves"] >= 1
    assert r.kpis["max_range_m"] <= SPEC.link_range_m
