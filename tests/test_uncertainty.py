"""Conformal prediction must actually deliver the coverage it promises.

These are statistical tests with fixed seeds and generous tolerances: they are
checking that the estimator is correct, not that a particular random draw came
out a particular way.
"""
import numpy as np
import pytest

from fa_core.uncertainty import (calibrate_classification, calibrate_regression,
                                 coverage_alarm, empirical_coverage,
                                 min_calibration_n, StratifiedCalibrator)

CLASSES = [f"c{i}" for i in range(6)]


def _softmax_data(rng, n, k=6):
    y = rng.integers(0, k, n)
    lg = rng.normal(0, 1, (n, k))
    lg[np.arange(n), y] += rng.uniform(0.5, 3.0, n)
    e = np.exp(lg - lg.max(1, keepdims=True))
    return e / e.sum(1, keepdims=True), y


@pytest.mark.parametrize("alpha", [0.05, 0.10, 0.20])
def test_regression_achieves_nominal_coverage(alpha):
    rng = np.random.default_rng(0)
    t = rng.uniform(0, 6, 4000); p = t + rng.normal(0, 0.4, 4000)
    cal = calibrate_regression(t, p, alpha=alpha)
    tt = rng.uniform(0, 6, 8000); pp = tt + rng.normal(0, 0.4, 8000)
    cov = empirical_coverage(tt, pp - cal.q, pp + cal.q)
    assert abs(cov - (1 - alpha)) < 0.02


def test_normalized_intervals_are_adaptive_and_still_cover():
    rng = np.random.default_rng(1)
    t = rng.uniform(0, 6, 4000)
    s = 0.1 + 0.2 * t
    p = t + rng.normal(0, s)
    cal = calibrate_regression(t, p, alpha=0.10, sigma=s)
    assert cal.normalized
    tt = rng.uniform(0, 6, 8000); ss = 0.1 + 0.2 * tt
    pp = tt + rng.normal(0, ss)
    cov = empirical_coverage(tt, pp - cal.q * ss, pp + cal.q * ss)
    assert abs(cov - 0.90) < 0.02


def test_lac_classification_coverage():
    rng = np.random.default_rng(2)
    p, y = _softmax_data(rng, 4000)
    cal = calibrate_classification(p, y, CLASSES, alpha=0.10, score="lac")
    pt, yt = _softmax_data(rng, 8000)
    cov = np.mean([CLASSES[t] in cal.predict_set(r) for r, t in zip(pt, yt)])
    assert abs(cov - 0.90) < 0.02


def test_prediction_set_is_never_empty():
    rng = np.random.default_rng(3)
    p, y = _softmax_data(rng, 2000)
    cal = calibrate_classification(p, y, CLASSES, alpha=0.10, score="lac")
    for r in p[:200]:
        assert len(cal.predict_set(r)) >= 1


def test_non_singleton_sets_route_to_review():
    rng = np.random.default_rng(4)
    p, y = _softmax_data(rng, 2000)
    cal = calibrate_classification(p, y, CLASSES, alpha=0.10, score="lac")
    ambiguous = np.full(len(CLASSES), 1 / len(CLASSES))
    assert cal.needs_review(ambiguous)
    confident = np.zeros(len(CLASSES)); confident[0] = 1.0
    assert not cal.needs_review(confident)


def test_insufficient_calibration_data_yields_infinite_quantile():
    """Below the finite-sample threshold the guarantee is not certifiable."""
    rng = np.random.default_rng(5)
    n = min_calibration_n(0.05) - 1
    cal = calibrate_regression(rng.normal(size=n), rng.normal(size=n), alpha=0.05)
    assert np.isinf(cal.q)


def test_stratified_falls_back_and_reports_it():
    rng = np.random.default_rng(6)
    p, y = _softmax_data(rng, 1000)
    g = calibrate_classification(p, y, CLASSES, alpha=0.10)
    s = StratifiedCalibrator(alpha=0.10, global_cal=g, min_n=500)
    s.strata[StratifiedCalibrator.key("rice", "tillering", "a")] = \
        calibrate_classification(p[:100], y[:100], CLASSES, alpha=0.10)
    cal, hit = s.get("rice", "tillering", "a")     # too few samples
    assert not hit and cal is g
    cal, hit = s.get("wheat", "boot", "z")          # stratum absent
    assert not hit and cal is g


def test_coverage_alarm_fires_only_on_real_degradation():
    assert coverage_alarm(0.70, 0.90, 1000)
    assert not coverage_alarm(0.899, 0.90, 1000)
