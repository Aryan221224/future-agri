"""The camera on the sprayer, simulated: what it sees along the flown track, with the
detector's measured recall and false positives at the flight's GSD."""
import datetime as dt

import numpy as np
import pytest

from fa_farm.truth import GroundTruthFarm
from fa_scan.sprayer_camera import SprayerCamera, cells_along, observe, to_cell_messages
from test_truth import NL_NORMALS, _spec, _weather


@pytest.fixture(scope="module")
def truth():
    t = GroundTruthFarm(_spec(w=150, h=100, cell=5.0, inoculum=False, seed=7),
                        _weather(dt.date(2025, 1, 1), 365, NL_NORMALS))
    t.advance_to(dt.date(2025, 6, 5))
    t.weeds = np.full(t.grid.n_cells, 20.0)                  # a known, weedy field
    return t


def test_footprint_and_gsd():
    cam = SprayerCamera()
    assert cam.footprint_m(3.0) == pytest.approx(4.2, abs=0.05)
    assert cam.gsd_m(3.0) == pytest.approx(0.0022, abs=0.0001)


def test_cells_along_one_pass(truth):
    g = truth.grid
    y = float(np.median(g.cells_xy[:, 1]))
    track = [{"x": g.cells_xy[:, 0].min(), "y": y, "sortie": 1, "spraying": True},
             {"x": g.cells_xy[:, 0].max(), "y": y, "sortie": 1, "spraying": True}]
    m = cells_along(track, g, width_m=g.cell_m)
    assert m.any() and np.all(np.abs(g.cells_xy[m, 1] - y) <= g.cell_m)


def test_cells_along_filters_sortie_and_spraying(truth):
    g = truth.grid
    x0, x1 = g.cells_xy[:, 0].min(), g.cells_xy[:, 0].max()
    ylo, yhi = np.quantile(g.cells_xy[:, 1], [0.1, 0.9])
    track = [{"x": x0, "y": ylo, "sortie": 1, "spraying": True}, {"x": x1, "y": ylo, "sortie": 1, "spraying": True},
             {"x": x1, "y": yhi, "sortie": 2, "spraying": False}, {"x": x0, "y": yhi, "sortie": 2, "spraying": True}]
    s1 = cells_along(track, g, g.cell_m, sortie=1)
    s2 = cells_along(track, g, g.cell_m, sortie=2)
    assert s1.any() and s2.any() and not (s1 & s2).any()
    ferry = cells_along(track[2:3] + track[2:3], g, g.cell_m, sortie=2, spraying_only=True)
    assert not ferry.any()


def test_observe_matches_truth_on_seen_cells_and_nan_elsewhere(truth):
    g, state = truth.grid, truth.state()
    seen = np.zeros(g.n_cells, bool)
    seen[: g.n_cells // 3] = True
    obs = observe(state, g, seen, gsd_m=0.0025, das=25, rng=np.random.default_rng(0))
    assert np.array_equal(obs.observed, seen)
    assert np.isnan(obs.weed_density_est[~seen]).all() and np.isinf(obs.weed_density_sd[~seen]).all()
    err = np.nanmean(obs.weed_density_est[seen]) - state.weeds_m2[seen].mean()
    assert abs(err) < 0.25 * state.weeds_m2[seen].mean() + 0.5


def test_too_coarse_gsd_is_uninformative(truth):
    g = truth.grid
    obs = observe(truth.state(), g, np.ones(g.n_cells, bool), gsd_m=0.05, das=25, rng=np.random.default_rng(0))
    assert np.isinf(obs.weed_density_sd).all()


def test_cell_messages_carry_the_schema_fields(truth):
    g = truth.grid
    seen = np.zeros(g.n_cells, bool)
    seen[:5] = True
    obs = observe(truth.state(), g, seen, gsd_m=0.0025, das=25, rng=np.random.default_rng(1))
    msgs = to_cell_messages(obs, g, t="2025-06-05T07:00:00Z")
    assert [m["cell"] for m in msgs] == [0, 1, 2, 3, 4]
    for m in msgs:
        assert set(m) == {"t", "cell", "weeds", "area_m2", "gsd_m", "blight"}
        assert m["weeds"] >= 0 and m["area_m2"] == pytest.approx(g.cell_m ** 2)
