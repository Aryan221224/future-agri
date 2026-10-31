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


# --- class-conditional + action-level gate (real-run finding: quickweed coverage 0.52) ---

from fa_core.uncertainty import (calibrate_action_gate, calibrate_class_conditional,  # noqa: E402
                                 action_grouping, recalibrate_with_field_labels,
                                 StratifiedCalibrator)

WCLASSES = ["maize", "amaranth", "barnyard", "quickweed", "background"]


def _weed_scores(n, rng):
    """A detector that is good on maize and poor on a rare weed it confuses with amaranth."""
    p_cls = np.array([0.45, 0.2, 0.15, 0.05, 0.15])
    y = rng.choice(5, n, p=p_cls)
    logits = rng.normal(0, 1.0, (n, 5))
    strength = np.array([4.0, 2.0, 1.6, 0.4, 1.5])
    logits[np.arange(n), y] += strength[y]
    rare = y == 3
    logits[rare, 1] += 1.5                       # quickweed looks like amaranth
    p = np.exp(logits)
    return p / p.sum(1, keepdims=True), y


def _coverage(cal_fn, probs, y, classes):
    sets = [cal_fn(p) for p in probs]
    cov = np.array([classes[t] in s for t, s in zip(y, sets)])
    size = np.array([len(s) for s in sets])
    return cov, size


def test_marginal_gate_hides_the_rare_class_class_conditional_does_not():
    # ~1000 rare-class examples per split: per-class coverage sd ~0.013, so a
    # 0.86 floor tests the method rather than one random draw.
    rng = np.random.default_rng(0)
    pc, yc = _weed_scores(20000, rng)
    pt, yt = _weed_scores(20000, rng)
    marg = calibrate_classification(pc, yc, WCLASSES, alpha=0.10, score="lac", randomized=False)
    mond = calibrate_class_conditional(pc, yc, WCLASSES, alpha=0.10)
    cov_m, _ = _coverage(marg.predict_set, pt, yt, WCLASSES)
    cov_c, _ = _coverage(mond.predict_set, pt, yt, WCLASSES)
    rare = yt == 3
    assert cov_m.mean() >= 0.87                          # marginal target met...
    assert cov_m[rare].mean() < 0.80                     # ...while the rare class fails
    for k in range(5):                                   # every class meets it with Mondrian
        assert cov_c[yt == k].mean() >= 0.86, (WCLASSES[k], cov_c[yt == k].mean())


def test_action_gate_cuts_review_rate_at_equal_coverage():
    rng = np.random.default_rng(1)
    pc, yc = _weed_scores(4000, rng)
    pt, yt = _weed_scores(4000, rng)
    fine = calibrate_class_conditional(pc, yc, WCLASSES, alpha=0.10)
    gate = calibrate_action_gate(pc, yc, WCLASSES, alpha=0.10)
    _, size_f = _coverage(fine.predict_set, pt, yt, WCLASSES)
    groups = action_grouping(WCLASSES)
    names = list(groups)
    truth_g = [next(g for g in names if WCLASSES[t] in groups[g]) for t in yt]
    sets_g = [gate.predict(p) for p in pt]
    cov_g = np.mean([t in s for t, s in zip(truth_g, sets_g)])
    review_g = np.mean([len(s) != 1 for s in sets_g])
    assert set(names) == {"crop", "weed", "background"}
    assert cov_g >= 0.87
    assert review_g < np.mean(size_f != 1)


def test_undersampled_class_goes_to_review_not_to_a_fake_guarantee():
    rng = np.random.default_rng(2)
    pc, yc = _weed_scores(400, rng)
    keep = ~((yc == 3) & (np.arange(len(yc)) % 10 != 0))    # leave ~2 quickweed
    cal = calibrate_class_conditional(pc[keep], yc[keep], WCLASSES, alpha=0.05)
    assert "quickweed" in cal.undercalibrated
    assert all("quickweed" in cal.predict_set(p) for p in pc[:20])


def test_field_recalibration_needs_enough_labels(tmp_path):
    rng = np.random.default_rng(3)
    pc, yc = _weed_scores(1000, rng)
    cal = calibrate_class_conditional(pc, yc, WCLASSES, alpha=0.10)
    kept, rep = recalibrate_with_field_labels(cal, pc[:50], yc[:50], WCLASSES, 0.10)
    assert kept is cal and not rep.used_field_labels
    new, rep = recalibrate_with_field_labels(cal, pc[:300], yc[:300], WCLASSES, 0.10)
    assert rep.used_field_labels and new is not cal
    sc = StratifiedCalibrator(alpha=0.10, global_cal=new)
    sc.to_json(tmp_path / "g.json")
    back = StratifiedCalibrator.from_json(tmp_path / "g.json")
    assert back.global_cal.predict_set(pc[0]) == new.predict_set(pc[0])
