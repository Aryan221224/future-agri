# 0012 — Conformal sets for detections, with background as a class

**Status:** accepted · 2026-09-22

## Context

ADR 0002 chose split conformal prediction over MC dropout, and every result so
far was on synthetic softmax vectors. A real detector changes three things:

1. RF-DETR emits per-class *sigmoids* (focal loss), not a softmax. A set needs
   one categorical distribution per detection.
2. A detection can be wrong about **being a plant at all**. Calibrating only on
   detections matched to ground truth certifies species, not existence, and says
   nothing about the false positives an operator actually sees.
3. Detections in one frame share light, stage and focus. They are not
   independent, so per-detection confidence intervals are too narrow.

## Decision

* Per detection, `p = normalise([s_1..s_K, prod(1 - s_k)])` over the K classes
  plus **background** (`fa_gateway.rfdetr.class_distribution`).
* Label every detection above the operating threshold with the class of the
  plant it matches (class-agnostic greedy IoU >= 0.5), or background. Calibrate
  LAC on validation detections, deterministic (ADR 0002's production default).
* The gateway drops a detection whose set is `{background}`, reviews one whose
  set contains background and a class ("may not be a plant"), and reviews any
  other non-singleton set as before.
* Coverage intervals come from a bootstrap over **images**, not detections.
* The operating threshold is chosen on validation (best F1); test is used once.
* Preprocessing and decoding are one NumPy implementation shared by calibration
  (PyTorch) and deployment (TensorRT / ONNX), checked for parity by `ml/export.py`.

## Consequences

The guarantee becomes the one an operator needs: for any detection reported,
the set contains what it really is — a species or "not a plant" — with
probability >= 1 − α, under exchangeability with the calibration images.
Missed plants (false negatives) are outside any set-valued guarantee and are
reported as recall. With 26 validation images the per-date strata hold a few
hundred detections each; the shift test (calibrate on early dates, test on
late) measures on real plants what `bench/conformal_coverage.py` showed on
synthetic data.
