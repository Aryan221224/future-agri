"""Edge pipeline: gating, aggregation and the uplink contract."""
import datetime as dt
import json
import numpy as np

from fa_core.uncertainty import (StratifiedCalibrator, calibrate_classification,
                                 calibrate_regression)
from fa_gateway.perception import StubDetector, build_detector
from fa_gateway.pipeline import EdgePipeline

CLASSES = ["crop", "broadleaf_weed", "grass_weed", "disease", "deficiency", "unknown"]


def _calibrator(alpha=0.10, seed=0):
    rng = np.random.default_rng(seed)
    n = 3000
    y = rng.integers(0, len(CLASSES), n)
    lg = rng.normal(0, 1, (n, len(CLASSES)))
    lg[np.arange(n), y] += rng.uniform(1.0, 3.5, n)
    e = np.exp(lg - lg.max(1, keepdims=True))
    cal = calibrate_classification(e / e.sum(1, keepdims=True), y, CLASSES,
                                   alpha=alpha, score="lac")
    c = StratifiedCalibrator(alpha=alpha, global_cal=cal, min_n=200)
    c.strata[StratifiedCalibrator.key("rice", "tillering", "s1")] = cal
    return c


def _frames(n=8, seed=0):
    rng = np.random.default_rng(seed)
    return [(rng.random((128, 128, 3)) * 255).astype(np.uint8) for _ in range(n)]


def test_detector_is_deterministic_for_the_same_frame():
    d = StubDetector(classes=CLASSES)
    img = _frames(1)[0]
    assert len(d(img).detections) == len(d(img).detections)


def test_build_detector_degrades_to_stub_without_weights():
    d = build_detector("auto", classes=CLASSES)
    assert isinstance(d, StubDetector)


def test_zone_summary_is_small_enough_for_a_rural_uplink():
    pipe = EdgePipeline(StubDetector(classes=CLASSES), _calibrator(), CLASSES,
                        crop="rice", stage="tillering", site="s1")
    s, _ = pipe.process_zone("z1", _frames(12), 4.0, dt.datetime.now(dt.timezone.utc))
    assert s.payload_bytes() < 4096
    json.loads(s.to_json())          # must be valid JSON


def test_uncalibrated_stratum_is_reported_not_hidden():
    pipe = EdgePipeline(StubDetector(classes=CLASSES), _calibrator(), CLASSES,
                        crop="wheat", stage="boot", site="unknown-site")
    s, _ = pipe.process_zone("z1", _frames(4), 1.0)
    assert s.calibrated is False
    assert "no calibration" in s.notes


def test_review_items_carry_a_reason_and_no_imagery():
    pipe = EdgePipeline(StubDetector(classes=CLASSES), _calibrator(), CLASSES,
                        crop="rice", stage="tillering", site="s1")
    _, reviews = pipe.process_zone("z1", _frames(16), 4.0)
    for r in reviews:
        assert r.reason
        assert len(r.label_set) != 1
        assert not hasattr(r, "image")   # the frame stays at the edge


def test_densities_are_per_hectare():
    pipe = EdgePipeline(StubDetector(classes=CLASSES), _calibrator(), CLASSES,
                        crop="rice", stage="tillering", site="s1")
    s1, _ = pipe.process_zone("z", _frames(8, seed=1), 1.0)
    s2, _ = pipe.process_zone("z", _frames(8, seed=1), 4.0)
    for k in s1.density_per_ha:
        assert s2.density_per_ha[k] < s1.density_per_ha[k]


def test_pipeline_without_calibrator_does_not_gate():
    pipe = EdgePipeline(StubDetector(classes=CLASSES), None, CLASSES)
    s, reviews = pipe.process_zone("z", _frames(6), 2.0)
    assert reviews == []
    assert np.isnan(s.coverage_target)


def test_action_gate_routes_on_crop_vs_weed_not_species():
    from fa_core.uncertainty import calibrate_action_gate
    classes = ["crop", "broadleaf_weed", "grass_weed", "background"]
    rng = np.random.default_rng(4)
    n = 4000
    y = rng.choice(4, n, p=[0.4, 0.25, 0.2, 0.15])
    lg = rng.normal(0, 1, (n, 4))
    lg[np.arange(n), y] += 2.5
    lg[(y == 1) | (y == 2), 1] += 1.0           # the two weeds are confused with each other
    lg[(y == 1) | (y == 2), 2] += 1.0
    p = np.exp(lg)
    p /= p.sum(1, keepdims=True)
    gate = calibrate_action_gate(p, y, classes, alpha=0.10)
    det = StubDetector(classes=classes)
    pipe_species = EdgePipeline(det, StratifiedCalibrator(
        alpha=0.10, global_cal=calibrate_classification(p, y, classes, alpha=0.10, score="lac",
                                                        randomized=False)), classes=classes)
    pipe_action = EdgePipeline(det, classes=classes, action_gate=gate)
    s1, r1 = pipe_species.process_zone("z", _frames(12), area_ha=1.0)
    s2, r2 = pipe_action.process_zone("z", _frames(12), area_ha=1.0)
    assert s2.action_counts is not None
    assert s2.calibration_stratum == "action"
    assert all(set(r.label_set) <= {"crop", "weed", "background"} for r in r2)
    json.loads(s2.to_json())                       # still a valid uplink payload
