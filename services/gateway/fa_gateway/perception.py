"""Edge perception: detection and retrieval, with a hard dependency boundary.

Model choice, and the licence reasoning behind it
-------------------------------------------------
RF-DETR (Apache-2.0, DINOv2 backbone, permissively-licensed weights) is the
primary detector. The alternatives were rejected on licence, not accuracy:

* Ultralytics YOLO is AGPL-3.0.
* YOLO-NAS ships Apache-2.0 *code* with proprietary non-commercial *weights* --
  worse than AGPL, because AGPL at least offers a compliance path.
* DEIMv2's licence forbids commercial use outright, despite being reported as
  Apache-2.0 on at least one widely-read model-licensing page.
* D-FINE is Apache-2.0 and is the A/B candidate, but its Objects365-pretrained
  checkpoints inherit that dataset's terms -- use the COCO-only weights.

CI enforces this: ``scripts/check_licences.py`` fails the build if any AGPL or
GPL package enters the dependency tree, because the practical risk is a
*transitive* one. The conformal-object-detection library CODS is MIT but hard-
depends on ultralytics, so installing it drags AGPL in sideways.

Runtime
-------
On Jetson the deployed path is a TensorRT engine built from ONNX, not PyTorch:
plain TensorRT rather than DeepStream (whose Python bindings are deprecated in
favour of pyservicemaker) and not ONNX Runtime (no official JetPack 7 aarch64
wheel). This module abstracts that behind ``Detector`` so the same gateway code
runs against PyTorch on a workstation, TensorRT on the aircraft, and a
deterministic stub in CI. The trained model and all three backends live in
:mod:`fa_gateway.rfdetr`; the training, calibration and export tooling is in
``ml/``.

**Engines are not portable.** Build them on the target board, at the target
JetPack. A JetPack 6 engine copied to a JetPack 7 device will not load.
"""
from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Detection:
    bbox: tuple[float, float, float, float]      # x1, y1, x2, y2 in pixels
    probs: np.ndarray                            # per-class probabilities
    score: float

    @property
    def top_class(self) -> int:
        return int(np.argmax(self.probs))


@dataclass
class FrameResult:
    frame_id: str
    detections: list[Detection]
    latency_ms: float
    model_version: str
    width: int = 0
    height: int = 0


class Detector(ABC):
    """A detector, whatever it runs on."""

    classes: list[str] = []
    model_version: str = "unknown"

    @abstractmethod
    def infer(self, image: np.ndarray) -> list[Detection]: ...

    def __call__(self, image: np.ndarray, frame_id: str = "") -> FrameResult:
        t0 = time.perf_counter()
        dets = self.infer(image)
        return FrameResult(
            frame_id=frame_id or f"f{int(t0 * 1e6)}",
            detections=dets,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            model_version=self.model_version,
            height=int(image.shape[0]), width=int(image.shape[1]),
        )


class StubDetector(Detector):
    """Deterministic synthetic detector.

    Exists so the whole pipeline -- conformal gating, review routing, MQTT
    publishing, the twin -- is testable in CI and demonstrable on a laptop with
    no model weights, no CUDA and no network. It is seeded from the image
    content, so the same frame always yields the same detections, which is what
    makes end-to-end tests meaningful rather than flaky.
    """

    def __init__(self, classes: list[str] | None = None, max_dets: int = 12,
                 model_version: str = "stub-v1"):
        self.classes = classes or ["crop", "broadleaf_weed", "grass_weed",
                                   "disease_lesion", "nutrient_deficiency", "unknown"]
        self.max_dets = max_dets
        self.model_version = model_version

    def infer(self, image: np.ndarray) -> list[Detection]:
        h, w = image.shape[:2]
        seed = int(hashlib.blake2b(np.ascontiguousarray(image).tobytes(),
                                   digest_size=8).hexdigest(), 16) % (2**32)
        rng = np.random.default_rng(seed)
        n = int(rng.integers(0, self.max_dets + 1))
        out: list[Detection] = []
        for _ in range(n):
            x1, y1 = rng.uniform(0, w * 0.9), rng.uniform(0, h * 0.9)
            bw, bh = rng.uniform(w * 0.02, w * 0.1), rng.uniform(h * 0.02, h * 0.1)
            logits = rng.normal(0, 1, len(self.classes))
            logits[rng.integers(0, len(self.classes))] += rng.uniform(0.3, 3.0)
            e = np.exp(logits - logits.max())
            p = e / e.sum()
            out.append(Detection((x1, y1, min(x1 + bw, w), min(y1 + bh, h)),
                                 p, float(p.max())))
        return out


def RFDETRRunDetector(run_dir, backend: str = "auto", threshold: float | None = None):
    """A trained RF-DETR from ``ml/runs/<name>`` on TensorRT, ONNX Runtime or PyTorch.

    Preprocessing, decoding and the class-plus-background probabilities live in
    :mod:`fa_gateway.rfdetr` so calibration (``ml/evaluate.py``) and the aircraft
    run the same code; only the forward pass differs between backends.
    """
    from .rfdetr import detector_from_run
    return detector_from_run(run_dir, backend=backend, threshold=threshold)


def build_detector(kind: str = "auto", run_dir: str | Path | None = None, **kw) -> Detector:
    """Pick a backend, preferring the fastest available and degrading loudly.

    With ``run_dir`` (an ``ml/runs/<name>`` directory) the trained RF-DETR is
    loaded on TensorRT, then ONNX Runtime, then PyTorch. Without it -- or if no
    backend can load -- the deterministic stub is returned with a warning.
    """
    if run_dir is not None and kind in ("auto", "tensorrt", "onnx", "torch"):
        try:
            return RFDETRRunDetector(run_dir, backend=kind, threshold=kw.get("threshold"))
        except Exception as e:
            if kind != "auto":
                raise
            log.info("trained detector unavailable (%s)", e)
    elif kind in ("tensorrt", "onnx", "torch"):
        raise ValueError(f"backend {kind!r} needs run_dir=<ml/runs/NAME>")
    log.warning("falling back to StubDetector -- results are synthetic and must "
                "not be used for any real recommendation")
    return StubDetector(classes=kw.get("classes"))
