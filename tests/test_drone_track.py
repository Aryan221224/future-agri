"""Flight track keyframes recorded by the mission simulator for 3D playback."""
import numpy as np
import pytest

from fa_drone.mission import MissionSettings, Weather, simulate_mission
from fa_drone.spec import load_spec
from test_drone_mission import _field

SPEC = load_spec()
CALM = Weather(1.5, 270.0, 2.5, 20.0)


@pytest.fixture(scope="module")
def flown():
    # 1.5 ha: several sorties (10 L at 15 L/ha covers ~0.67 ha per tank)
    grid = _field(150, 100, cell=5.0)
    res = simulate_mission(grid, SPEC, MissionSettings(speed_ms=5.0, swath_m=4.0, rate_l_ha=15.0),
                           CALM, seed=3)
    return grid, res


def test_track_is_time_ordered_and_ends_landed(flown):
    _, res = flown
    tr = res.track
    assert len(tr) > 20
    t = [k["t"] for k in tr]
    assert all(b >= a for a, b in zip(t, t[1:]))
    assert tr[0]["z"] == 0.0 and tr[0]["phase"] == "takeoff"
    assert tr[-1]["phase"] == "landed" and tr[-1]["z"] == 0.0
    assert set(tr[0]) >= {"t", "x", "y", "z", "soc", "v_cell", "liquid_l", "spraying",
                          "flow_lpm", "phase", "sortie"}


def test_spray_keyframes_inside_field_bounds(flown):
    grid, res = flown
    minx, miny, maxx, maxy = grid.bounds_local
    spray = [k for k in res.track if k["spraying"]]
    assert spray
    for k in spray:
        assert minx - 10 <= k["x"] <= maxx + 10 and miny - 10 <= k["y"] <= maxy + 10
        assert k["flow_lpm"] > 0 and k["z"] == pytest.approx(2.5)


def test_kpis_identical_with_and_without_track():
    grid = _field(80, 60, cell=5.0)
    s = MissionSettings()
    a = simulate_mission(grid, SPEC, s, CALM, seed=1, record_track=True)
    b = simulate_mission(grid, SPEC, s, CALM, seed=1, record_track=False)
    assert a.kpis == b.kpis
    assert np.allclose(a.dose_l_ha, b.dose_l_ha)
    assert b.track == []


def test_sortie_increments_after_rth(flown):
    _, res = flown
    assert res.kpis["sorties"] >= 2
    assert max(k["sortie"] for k in res.track) == res.kpis["sorties"]
    assert any(k["phase"] == "ground" for k in res.track)


def test_liquid_never_increases_within_a_sortie(flown):
    _, res = flown
    by = {}
    for k in res.track:
        by.setdefault(k["sortie"], []).append(k["liquid_l"])
    for s, ls in by.items():
        # the refill happens on the ground at the end of a sortie, never during one
        assert all(b <= a + 1e-9 for a, b in zip(ls, ls[1:])), s
