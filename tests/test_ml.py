"""Perception training tooling: data conversion, matching, the detection gate.

No torch, no weights, no network: the model is replaced by synthetic queries
whose probabilities carry known information, so the calibration arithmetic is
tested against a known answer.
"""
import json

import numpy as np
import pytest
from PIL import Image

from fa_gateway.perception import Detection, FrameResult
from fa_gateway.pipeline import EdgePipeline
from fa_gateway.rfdetr import (BACKGROUND, RFDETRDetector, class_distribution, cxcywh_to_xyxy,
                               preprocess, resize_bilinear)
from fa_core.uncertainty import StratifiedCalibrator, calibrate_classification
from fa_ml import detconf, synthetic, weedsgalore
from fa_ml.matching import greedy_match, iou_matrix, label_detections


# ------------------------------------------------------------------ conversion

def test_conversion_writes_coco_that_rfdetr_can_read(tmp_path):
    raw = synthetic.make(tmp_path / "raw", n=(4, 2, 2), size=64)
    meta = weedsgalore.convert(raw, tmp_path / "coco")
    for split in ("train", "valid", "test"):
        coco = json.loads((tmp_path / "coco" / split / "_annotations.coco.json").read_text(encoding="utf-8"))
        assert [c["id"] for c in coco["categories"]] == [1, 2, 3, 4, 5]
        assert coco["licenses"][0]["name"] == "CC BY 4.0"
        assert all("date" in im for im in coco["images"])
        for a in coco["annotations"]:
            x, y, w, h = a["bbox"]
            assert w > 0 and h > 0 and x >= 0 and y >= 0 and x + w <= 64 and y + h <= 64
    assert meta["splits"]["train"]["images"] == 4
    assert (tmp_path / "coco" / "preview_train.jpg").exists()


def test_instance_boxes_are_tight_and_classed_by_majority():
    sem = np.zeros((20, 20), np.uint8)
    inst = np.zeros((20, 20), np.uint16)
    inst[2:6, 3:9] = 7
    sem[2:6, 3:9] = 2
    sem[2, 3] = 1                       # one stray pixel of another class
    inst[10:15, 10:12] = 9
    sem[10:15, 10:12] = 3
    out = sorted(weedsgalore.instances_from_masks(inst, sem, min_area=4))
    assert out[0] == (2, [3.0, 2.0, 6.0, 4.0], 24)
    assert out[1] == (3, [10.0, 10.0, 2.0, 5.0], 10)


def test_missing_instance_masks_fall_back_to_components():
    sem = np.zeros((20, 20), np.uint8)
    sem[1:4, 1:4] = 1
    sem[10:13, 10:14] = 1
    out = weedsgalore.instances_from_masks(None, sem, min_area=4)
    assert len(out) == 2 and all(c == 1 for c, _, _ in out)


def test_rgb_instance_encoding_is_decoded():
    inst = np.zeros((8, 8, 3), np.uint8)
    inst[1:3, 1:3] = (0, 1, 2)
    inst[5:7, 5:8] = (3, 0, 0)
    sem = np.where(inst.any(-1), 4, 0).astype(np.uint8)
    assert len(weedsgalore.instances_from_masks(inst, sem, min_area=1)) == 2


# ------------------------------------------------------------------ geometry

def test_iou_and_greedy_match_take_each_plant_once():
    gt = np.array([[0, 0, 10, 10], [20, 20, 30, 30]], float)
    pred = np.array([[0, 0, 10, 10], [1, 1, 10, 10], [50, 50, 60, 60]], float)
    assert iou_matrix(pred, gt)[0, 0] == pytest.approx(1.0)
    m = greedy_match(pred, np.array([0.9, 0.8, 0.7]), gt)
    assert m.tolist() == [0, -1, -1]           # duplicate and stray are unmatched
    lab, _ = label_detections(pred, np.array([0.9, 0.8, 0.7]), gt, np.array([3, 1]), background_index=5)
    assert lab.tolist() == [3, 5, 5]


def test_boxes_decode_to_pixels_and_clip():
    b = cxcywh_to_xyxy(np.array([[0.5, 0.5, 0.2, 0.4], [0.99, 0.5, 0.2, 0.2]]), 600, 300)
    assert b[0].tolist() == pytest.approx([240, 90, 360, 210])
    assert b[1, 2] == 600


# ------------------------------------------------------------------ scores

def test_class_distribution_puts_mass_on_background_when_nothing_fires():
    p = class_distribution(np.array([[-6.0, -6.0, -6.0, 0.0], [4.0, -4.0, -4.0, 0.0]]), 3)
    assert p.shape == (2, 4)
    assert np.allclose(p.sum(1), 1)
    assert p[0].argmax() == 3                  # background
    assert p[1].argmax() == 0


def test_preprocess_matches_expected_layout():
    img = (np.random.default_rng(0).random((600, 600, 3)) * 255).astype(np.uint8)
    x = preprocess(img, 608)
    assert x.shape == (3, 608, 608) and x.dtype == np.float32
    same = resize_bilinear(img.astype(np.float32), 600, 600)
    assert np.array_equal(same, img.astype(np.float32))


# ------------------------------------------------------------------ gate on synthetic queries

K = 5
CLASSES = weedsgalore.CLASSES + [BACKGROUND]


def _fake(split_name, n_img, rng, noise=1.0, dates=("2023-05-25", "2023-06-15")):
    """A Split and Preds pair with Q=40 queries per image: 12 true plants + clutter."""
    images, gt, bx, cs, pr, dl = [], {}, [], [], [], []
    for i in range(n_img):
        date = dates[i % len(dates)]
        n = 12
        xy = rng.uniform(0, 540, (n, 2))
        g = np.concatenate([xy, xy + rng.uniform(20, 50, (n, 2))], 1).astype(np.float32)
        gl = rng.integers(0, K, n)
        images.append({"id": i + 1, "file_name": f"{i}.png", "width": 600, "height": 600, "date": date})
        gt[i + 1] = (g, gl)
        logits = rng.normal(-3, 1.0, (40, K + 1))
        boxes = np.zeros((40, 4), np.float32)
        boxes[:n] = g + rng.normal(0, 1.5, g.shape)
        logits[np.arange(n), gl] += 5.0 + rng.normal(0, noise * 1.6, n)       # true plants
        confuse = rng.integers(0, K, n)
        logits[np.arange(n), confuse] += rng.normal(0, noise * 1.6, n)       # species confusion
        fp = rng.uniform(0, 560, (28, 2))
        boxes[n:] = np.concatenate([fp, fp + 30], 1)
        logits[np.arange(n, n + 10), rng.integers(0, K, 10)] += rng.uniform(1.0, 3.5, 10)  # clutter
        from fa_gateway.rfdetr import sigmoid
        bx.append(boxes)
        cs.append(sigmoid(logits[:, :K]))
        pr.append(class_distribution(logits, K))
        dl.append(date)
    split = detconf.Split(split_name, None, list(weedsgalore.CLASSES), images, gt,
                          {c + 1: c for c in range(K)})
    return split, detconf.Preds(np.arange(1, n_img + 1), dl, np.stack(bx), np.stack(cs), np.stack(pr))


def test_gate_delivers_nominal_coverage_on_exchangeable_data():
    rng = np.random.default_rng(1)
    sv, pv = _fake("valid", 120, rng)
    st, pt = _fake("test", 240, rng)
    thr, _ = detconf.choose_threshold(sv, pv)
    vP, vL, vD, vst = detconf.detections(sv, pv, thr)
    tP, tL, tD, _ = detconf.detections(st, pt, thr)
    assert vst["recall"] > 0.7 and (vL == K).any()          # plants found, clutter labelled bg
    groups = detconf.detection_images(st, pt, thr)
    reviews = []
    for alpha in (0.05, 0.10, 0.20):
        cal = detconf.calibrate(vP, vL, CLASSES, alpha)
        s = detconf.set_stats(cal, tP, tL, CLASSES, groups=groups)
        assert s["coverage"] >= 1 - alpha - 0.03, (alpha, s)
        assert s["coverage"] <= 1 - alpha + 0.06, (alpha, s)
        lo, hi = s["coverage_ci95"]
        assert lo <= s["coverage"] <= hi
        reviews.append(s["review_rate"])
    assert 0 < reviews[0] < 1 and reviews == sorted(reviews, reverse=True)


def test_date_shift_breaks_coverage_and_the_monitor_fires():
    rng = np.random.default_rng(2)
    sv, pv = _fake("valid", 160, rng, noise=0.6)
    early = _fake("test", 120, rng, noise=0.6, dates=("2023-05-25",))
    late = _fake("test", 120, rng, noise=2.2, dates=("2023-06-15",))
    val = detconf.detections(sv, pv, 0.3)[:3]
    ep, el, ed, _ = detconf.detections(*early, 0.3)
    lp, ll, ld, _ = detconf.detections(*late, 0.3)
    test = (np.concatenate([ep, lp]), np.concatenate([el, ll]), np.concatenate([ed, ld]))
    r = detconf.shift_experiment(val, test, CLASSES, 0.10, n_early=1)
    assert r["by_date"]["2023-05-25"]["alarm"] is False
    assert r["by_date"]["2023-06-15"]["alarm"] is True
    assert r["by_date"]["2023-06-15"]["coverage"] < r["by_date"]["2023-05-25"]["coverage"]


def test_stratified_calibration_round_trips_for_the_gateway(tmp_path):
    rng = np.random.default_rng(3)
    sv, pv = _fake("valid", 60, rng)
    P, L, D, _ = detconf.detections(sv, pv, 0.3)
    sc = detconf.stratified(P, L, D, CLASSES, 0.10, min_n=50)
    sc.to_json(tmp_path / "cal.json")
    back = StratifiedCalibrator.from_json(tmp_path / "cal.json")
    cal, hit = back.get(detconf.CROP, "2023-05-25", detconf.SITE)
    assert hit and cal.classes == CLASSES
    _, hit = back.get(detconf.CROP, "2024-01-01", detconf.SITE)
    assert hit is False


# ------------------------------------------------------------------ edge integration

class _FakeBackend:
    name = "fake"

    def __init__(self, logits, boxes):
        self.logits, self.boxes = logits, boxes

    def forward(self, batch):
        return self.boxes[None], self.logits[None]


def test_rfdetr_detector_and_pipeline_drop_confident_background():
    logits = np.full((4, K + 1), -8.0)
    logits[0, 1] = 6.0                     # confident amaranth
    logits[1, 0] = 0.3                     # weak maize: bg-ish, above threshold
    logits[2, :] = -8.0                    # nothing
    boxes = np.array([[0.2, 0.2, 0.1, 0.1], [0.6, 0.6, 0.1, 0.1], [0.5, 0.5, 0.1, 0.1],
                      [0.1, 0.9, 0.05, 0.05]], np.float32)
    det = RFDETRDetector(_FakeBackend(logits, boxes), weedsgalore.CLASSES, 64, threshold=0.5)
    img = np.zeros((100, 100, 3), np.uint8)
    res = det(img)
    assert isinstance(res, FrameResult) and all(isinstance(d, Detection) for d in res.detections)
    assert len(res.detections) == 2 and det.classes[-1] == BACKGROUND
    assert res.detections[0].probs.shape == (K + 1,)
    # A gate calibrated so that low-confidence detections resolve to {background}.
    rng = np.random.default_rng(0)
    y = rng.integers(0, K + 1, 2000)
    p = np.full((2000, K + 1), 0.02)
    p[np.arange(2000), y] = 0.9
    cal = calibrate_classification(p / p.sum(1, keepdims=True), y, det.classes, alpha=0.1,
                                   score="lac", randomized=False)
    sc = StratifiedCalibrator(alpha=0.1, global_cal=cal)
    pipe = EdgePipeline(det, sc, det.classes)
    s, reviews = pipe.process_zone("z", [img], 1.0)
    assert BACKGROUND not in s.class_counts
    assert s.class_counts.get("amaranth") == 1


def test_gate_comparison_on_synthetic_detections():
    """ml/gate_eval.evaluate_gates: Mondrian fixes the rare class, action level cuts review."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "gate_eval", Path(__file__).resolve().parents[1] / "ml" / "gate_eval.py")
    ge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ge)
    rng = np.random.default_rng(0)
    classes = ["maize", "amaranth", "quickweed", "background"]

    def draw(n):
        y = rng.choice(4, n, p=[0.5, 0.25, 0.08, 0.17])
        lg = rng.normal(0, 1, (n, 4))
        lg[np.arange(n), y] += np.array([4.0, 2.0, 0.3, 1.5])[y]
        lg[y == 2, 1] += 1.5
        p = np.exp(lg)
        return p / p.sum(1, keepdims=True), y

    vp, vl = draw(8000)
    tp, tl = draw(8000)
    r = ge.evaluate_gates(vp, vl, tp, tl, classes, 0.10, test_groups=np.arange(8000) // 20)
    assert r["marginal_lac"]["worst_class_coverage"] < 0.8
    assert r["class_conditional"]["worst_class_coverage"] > 0.85
    assert r["action_level"]["review_rate"] < r["class_conditional"]["review_rate"]
    assert r["marginal_lac"]["coverage_ci95"] is not None
