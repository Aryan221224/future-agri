"""Review findings on the closed loop, each pinned by a test (C = critical, I = important)."""
import dataclasses

import numpy as np
import pytest

from closedloop_cases import clone, session_on_flyable_day, session_with_herbicide_op
from fa_runner.closedloop.run import fly_closed_loop
from fa_scan.digital import build_digital_farm


@pytest.fixture(scope="module")
def base():
    return session_on_flyable_day()


@pytest.fixture
def case(base):
    s, op = base
    c = clone(s)
    return c, c.ops[op.id]


def test_c1_partial_scan_without_cover_and_stage_leaves_the_crop_state_alone():
    """A camera that reported only weed counts must not move the twin's stage or leaf area."""
    s, _ = session_with_herbicide_op()
    s.twin.field.advance_to(s.day)
    before = dict(s.twin.field.state())
    obs = s.world.scan(s.day, s.scan_gsd_m, s.camera, np.random.default_rng(3), s.archetype.crop)
    seen = np.zeros(s.grid.n_cells, bool)
    seen[:60] = True
    obs = dataclasses.replace(obs, observed=seen, cc_cells=np.full(s.grid.n_cells, 0.5),
                              dvs_cells=np.full(s.grid.n_cells, 1.0), crop_observed=False,
                              weed_density_est=np.where(seen, obs.weed_density_est, np.nan))
    s.twin.ingest(build_digital_farm(obs, s.grid), s.day)
    after = s.twin.field.state()
    assert after["DVS"] == pytest.approx(before["DVS"]) and after["LAI"] == pytest.approx(before["LAI"])
    assert np.isfinite(s.twin.digital.weed_map).all()


def test_c2_a_failing_listener_never_splits_the_session_from_its_log(case):
    s, op = case

    def boom(rep):
        raise RuntimeError("cancelled")
    s.closed_loop_listener = boom
    out = s.fly_closed_loop(op.id)
    r = clone(s)
    assert out["flown"] and op.status == "flown"
    assert r.st["litres"] == pytest.approx(s.st["litres"]) and r.ops[op.id].status == s.ops[op.id].status


def test_i1_refly_tops_up_and_never_goes_far_over_the_rate(case):
    s, op = case
    res = fly_closed_loop(s, op)
    rate = s.flight.rate_l_ha
    tg = res.prediction.targets
    assert (res.dose_l_ha[tg] > 1.5 * rate).mean() <= 0.05
    assert res.dose_l_ha.max() <= 1.6 * rate


def test_i2_cells_the_valve_closed_on_are_done_not_reflown(case, monkeypatch):
    s, op = case
    other = clone(s)
    for x in (s, other):
        monkeypatch.setattr(x.twin, "herbicide_targets", lambda w: ("blanket", np.ones(len(w), bool)))
    no_valve = fly_closed_loop(other, other.ops[op.id], valve=False)
    with_valve = fly_closed_loop(s, op, valve=True)
    assert len(with_valve.sorties) <= len(no_valve.sorties)
    assert with_valve.kpis["time_min"] <= no_valve.kpis["time_min"] + 0.01


def test_i8_partial_scan_leaves_no_nan_or_unknown_zone_in_what_the_views_use():
    s, _ = session_with_herbicide_op()
    obs = s.world.scan(s.day, s.scan_gsd_m, s.camera, np.random.default_rng(4), s.archetype.crop)
    seen = np.zeros(s.grid.n_cells, bool)
    seen[:40] = True
    obs = dataclasses.replace(obs, observed=seen, crop_observed=False,
                              weed_density_est=np.where(seen, obs.weed_density_est, np.nan))
    s.scan_now(obs=obs, ref="scan_x")
    assert np.isfinite(s.last_digital.weed_map).all()
    assert np.isfinite(s.events[-1]["weeds_m2"])
    s.world.truth_state = lambda: None                       # as on a live field: views come from scans
    s._snapshot(s.day)
    snap = s.snapshots[s.day.isoformat()]
    assert all(np.isfinite(snap[k].astype(float)).all() for k in ("lai", "cc", "weeds"))
