"""The prediction frozen before take-off, and what counts as a deviation from it."""
import json

import numpy as np
import pytest

from closedloop_cases import session_with_herbicide_op
from fa_runner.closedloop.prediction import Prediction, deviation_dose, deviation_weeds, freeze_prediction


def _pred(n=None, **kw):
    n = n or len(next(v for v in kw.values() if isinstance(v, np.ndarray)))
    base = dict(op_id="op", product="herbicide", weeds_mean=np.zeros(n), weeds_sd=np.full(n, np.inf),
                cc_mean=0.5, cc_sd=0.08, targets=np.ones(n, bool), dose_l_ha=np.full(n, 20.0),
                sorties=1, time_min=10.0, litres=10.0, made_at="2025-06-04")
    base.update(kw)
    return Prediction(**base)


def test_weed_z_score_flags_only_real_deviation():
    pred = _pred(weeds_mean=np.full(100, 10.0), weeds_sd=np.full(100, 2.0))
    obs = np.full(100, 10.5)
    obs[:20] = 30.0
    d = deviation_weeds(pred, obs, np.full(100, 1.0), np.ones(100, bool))
    assert d.significant and set(d.cells.tolist()) == set(range(20)) and "re-plan" in d.correction


def test_noise_is_not_chased():
    pred = _pred(weeds_mean=np.full(100, 10.0), weeds_sd=np.full(100, 2.0))
    obs = 10 + np.random.default_rng(0).normal(0, 1.5, 100)
    assert not deviation_weeds(pred, obs, np.full(100, 1.0), np.ones(100, bool)).significant


def test_few_cells_are_not_enough():
    pred = _pred(weeds_mean=np.full(1000, 10.0), weeds_sd=np.full(1000, 1.0))
    obs = np.full(1000, 10.0)
    obs[:4] = 50.0                                          # 4 cells < 5
    assert not deviation_weeds(pred, obs, np.ones(1000), np.ones(1000, bool)).significant


def test_five_percent_of_the_observed_cells_is_required():
    pred = _pred(weeds_mean=np.full(1000, 10.0), weeds_sd=np.full(1000, 1.0))
    obs = np.full(1000, 10.0)
    obs[:30] = 50.0                                         # 30 >= 5 cells but 3 % < 5 %
    assert not deviation_weeds(pred, obs, np.ones(1000), np.ones(1000, bool)).significant


def test_unobserved_cells_are_not_scored():
    pred = _pred(weeds_mean=np.full(100, 10.0), weeds_sd=np.full(100, 1.0))
    obs = np.full(100, np.nan)
    obs[:10] = 40.0
    seen = np.zeros(100, bool)
    seen[:10] = True
    d = deviation_weeds(pred, obs, np.full(100, 1.0), seen)
    assert d.significant and set(d.cells.tolist()) == set(range(10))


def test_no_map_means_no_deviation():
    pred = _pred(weeds_mean=np.zeros(50), weeds_sd=np.full(50, np.inf))
    assert not deviation_weeds(pred, np.full(50, 99.0), np.ones(50), np.ones(50, bool)).significant


def test_underdosed_flown_cells_are_a_dose_deviation():
    pred = _pred(targets=np.ones(100, bool), dose_l_ha=np.full(100, 20.0))
    dose = np.full(100, 20.0)
    dose[:10] = 8.0
    d = deviation_dose(pred, dose, flown=np.ones(100, bool))
    assert d.significant and set(d.cells.tolist()) == set(range(10)) and "re-fly" in d.correction


@pytest.fixture(scope="module")
def case():
    return session_with_herbicide_op()


def test_freeze_prediction_on_a_virtual_farm(case):
    s, op = case
    rng_before = s.rng.bit_generator.state
    p = freeze_prediction(s, op)
    assert p.litres > 0 and p.sorties >= 1 and len(p.weeds_mean) == s.grid.n_cells
    assert np.array_equal(p.targets, op.targets) and 0 < p.cc_mean < 1
    assert json.loads(json.dumps(p.to_json()))["op_id"] == op.id
    assert s.rng.bit_generator.state == rng_before          # freezing must not change the season
