"""Partial scans: a cell the camera did not cover keeps what the twin knew; it never reads
as 'no weeds'."""
import dataclasses

import numpy as np
import pytest

from closedloop_cases import first_quarter, half_mask, partial_obs, session_with_herbicide_op
from fa_scan.digital import build_digital_farm


@pytest.fixture(scope="module")
def case():
    return session_with_herbicide_op()


def test_digital_farm_zones_use_observed_cells_only(case):
    s, _ = case
    obs = s.world.scan(s.day, s.scan_gsd_m, s.camera, np.random.default_rng(1), s.archetype.crop)
    m = half_mask(s.grid)
    obs = dataclasses.replace(obs, observed=m, cc_cells=np.where(m, 0.6, 0.0),
                              weed_density_est=np.where(m, obs.weed_density_est, np.nan))
    df = build_digital_farm(obs, s.grid)
    cc = sum(z.cc * z.area_ha for z in df.zones) / sum(z.area_ha for z in df.zones)
    assert cc == pytest.approx(0.6, abs=0.02)               # the unobserved 0.0s are not averaged in
    assert np.isnan(df.weed_map[~m]).all()                   # unobserved = unknown, not zero
    assert sum(len(z.cells) for z in df.zones) == int(m.sum())


def test_unobserved_cells_keep_prior(case):
    s, _ = case
    prior = s.twin.digital.weed_map.copy()
    m = first_quarter(s.grid)
    obs = partial_obs(s, observed=m, weeds_m2=0.0)          # the observed quarter is now clean
    s.twin.ingest(build_digital_farm(obs, s.grid), s.day)
    assert np.allclose(s.twin.digital.weed_map[~m], prior[~m])
    assert np.allclose(s.twin.digital.weed_map[m], 0.0, atol=0.5)
    s.twin.ingest(build_digital_farm(dataclasses.replace(obs, weed_density_est=np.where(m, np.nan, 3.0),
                                                        observed=~m), s.grid), s.day)
    assert np.allclose(s.twin.digital.weed_map[m], 0.0, atol=0.5)   # still the quarter seen earlier


def test_full_scan_without_mask_is_unchanged(case):
    s, _ = case
    obs = s.world.scan(s.day, s.scan_gsd_m, s.camera, np.random.default_rng(2), s.archetype.crop)
    assert obs.observed is None
    df = build_digital_farm(obs, s.grid)
    assert np.allclose(df.weed_map, obs.weed_density_est) and sum(len(z.cells) for z in df.zones) == s.grid.n_cells


def test_herbicide_targets_follow_the_planner_rule(case):
    tw = case[0].twin
    n = len(tw.digital.weed_map)
    opt, tg = tw.herbicide_targets(np.full(n, 0.0))
    assert opt == "none" and not tg.any()
    opt, tg = tw.herbicide_targets(np.full(n, 40.0))
    assert opt == "blanket" and tg.all()


def test_herbicide_targets_reproduce_the_proposed_spot_job():
    s, op = session_with_herbicide_op()                     # fresh: the map that made the proposal
    opt, tg = s.twin.herbicide_targets(s.twin.digital.weed_map)
    assert opt == "spot" and np.array_equal(tg, op.targets)
