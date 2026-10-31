"""RF-DETR at the edge: one preprocessing and one decoding path for every backend.

Why this module exists
----------------------
The conformal gate is only as honest as the probabilities it is calibrated on.
If calibration runs through ``rfdetr.predict()`` on a workstation and the
aircraft runs a TensorRT engine through hand-written glue, the two paths differ
in resizing, score definition and class layout, and the coverage guarantee
silently stops applying to what flies. So there is exactly one path:

    image --preprocess()--> tensor --backend.forward()--> (boxes, logits)
          --decode()--> [Detection(probs over classes + background)]

and only ``backend.forward`` changes between PyTorch (calibration, workstation),
ONNX Runtime (laptop, CI parity) and TensorRT (A6000 benchmark, Jetson).
``ml/evaluate.py`` calibrates through this module; ``fa_gateway.pipeline`` gates
through it. The preprocessing and decoding are pure NumPy so the edge image
needs neither torch nor rfdetr.

Score definition
----------------
RF-DETR is trained with a sigmoid focal loss: each of Q queries emits K
independent class sigmoids ``s_k`` (plus one unused slot). A conformal set needs
one categorical distribution per detection, and a detection can be *wrong about
being a plant at all*, so the distribution is over K classes plus
``background``:

    w_k  = s_k                      k = 1..K
    w_bg = prod_k (1 - s_k)         "none of the classes is present"
    p    = w / sum(w)

Calibrating on matched detections labelled with their class, and unmatched
detections labelled ``background``, gives the guarantee operators need: for any
detection the edge reports, the set contains what it really is -- one of the
classes or "not a plant" -- with probability at least 1 - alpha.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

MEANS = np.array([0.485, 0.456, 0.406], np.float32)
STDS = np.array([0.229, 0.224, 0.225], np.float32)
BACKGROUND = "background"


# ---------------------------------------------------------------------------
# Preprocessing (bit-compatible with rfdetr.predict: bilinear, no antialias)
# ---------------------------------------------------------------------------

def _resize_axis_weights(n_in: int, n_out: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Source indices and weights for half-pixel-centre bilinear resampling.

    Matches torch ``F.interpolate(mode="bilinear", align_corners=False,
    antialias=False)`` and OpenCV ``INTER_LINEAR``, which is what RF-DETR uses
    for both training (Albumentations) and ``predict()``.
    """
    scale = n_in / n_out
    x = (np.arange(n_out, dtype=np.float64) + 0.5) * scale - 0.5
    x = np.clip(x, 0, None)
    x0 = np.floor(x).astype(np.int64)
    x0 = np.minimum(x0, n_in - 1)
    x1 = np.minimum(x0 + 1, n_in - 1)
    w1 = (x - x0).astype(np.float32)
    return x0, x1, w1


def resize_bilinear(img: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """HWC float32 bilinear resize without antialiasing."""
    h, w = img.shape[:2]
    if (h, w) == (out_h, out_w):
        return img.astype(np.float32, copy=False)
    y0, y1, wy = _resize_axis_weights(h, out_h)
    x0, x1, wx = _resize_axis_weights(w, out_w)
    img = img.astype(np.float32, copy=False)
    top = img[y0][:, x0] * (1 - wx)[None, :, None] + img[y0][:, x1] * wx[None, :, None]
    bot = img[y1][:, x0] * (1 - wx)[None, :, None] + img[y1][:, x1] * wx[None, :, None]
    return top * (1 - wy)[:, None, None] + bot * wy[:, None, None]


def preprocess(image: np.ndarray, resolution: int) -> np.ndarray:
    """RGB uint8/float HWC image -> normalised float32 CHW at ``resolution``."""
    img = np.asarray(image)
    if img.ndim == 2:
        img = np.repeat(img[:, :, None], 3, axis=2)
    if img.shape[2] == 4:
        img = img[:, :, :3]
    img = img.astype(np.float32)
    if img.max() > 1.0 + 1e-6:
        img = img / 255.0
    img = resize_bilinear(img, resolution, resolution)
    img = (img - MEANS) / STDS
    return np.ascontiguousarray(img.transpose(2, 0, 1), dtype=np.float32)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def sigmoid(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, np.float64)
    return (1.0 / (1.0 + np.exp(-np.clip(x, -60, 60)))).astype(np.float32)


def class_distribution(logits: np.ndarray, n_classes: int) -> np.ndarray:
    """(Q, K+extra) logits -> (Q, K+1) categorical over classes + background."""
    s = sigmoid(np.asarray(logits)[..., :n_classes])
    bg = np.prod(1.0 - s, axis=-1, keepdims=True)
    w = np.concatenate([s, bg], axis=-1)
    return (w / np.clip(w.sum(-1, keepdims=True), 1e-12, None)).astype(np.float32)


def cxcywh_to_xyxy(boxes: np.ndarray, width: int, height: int) -> np.ndarray:
    b = np.asarray(boxes, np.float32)
    cx, cy, bw, bh = b[..., 0], b[..., 1], b[..., 2], b[..., 3]
    out = np.stack([(cx - bw / 2) * width, (cy - bh / 2) * height,
                    (cx + bw / 2) * width, (cy + bh / 2) * height], axis=-1)
    out[..., [0, 2]] = np.clip(out[..., [0, 2]], 0, width)
    out[..., [1, 3]] = np.clip(out[..., [1, 3]], 0, height)
    return out


@dataclass
class DecodedQueries:
    """Every query of one image, before any threshold. Used for calibration."""
    boxes_xyxy: np.ndarray      # (Q, 4) pixels in the original image
    class_scores: np.ndarray    # (Q, K) independent sigmoids
    probs: np.ndarray           # (Q, K+1) categorical incl. background

    @property
    def score(self) -> np.ndarray:
        """Detection confidence: the best class sigmoid."""
        return self.class_scores.max(axis=1)


def decode_queries(boxes: np.ndarray, logits: np.ndarray, n_classes: int,
                   width: int, height: int) -> DecodedQueries:
    boxes = np.asarray(boxes)
    logits = np.asarray(logits)
    if boxes.ndim == 3:
        boxes, logits = boxes[0], logits[0]
    return DecodedQueries(
        boxes_xyxy=cxcywh_to_xyxy(boxes, width, height),
        class_scores=sigmoid(logits[:, :n_classes]),
        probs=class_distribution(logits, n_classes),
    )


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class Backend(Protocol):
    name: str

    def forward(self, batch: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(B,3,H,W) float32 -> (boxes (B,Q,4) cxcywh in [0,1], logits (B,Q,C))."""
        ...


class TorchBackend:
    """The rfdetr PyTorch module, called raw (no rfdetr post-processing)."""
    name = "torch"

    def __init__(self, rf, device: str | None = None, fp16: bool = False):
        import torch
        self.torch = torch
        self.module = rf.model.model
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        self.module.to(self.device).eval()
        self.fp16 = bool(fp16 and self.device.type == "cuda")
        if self.fp16:
            self.name = "torch-fp16"

    def forward(self, batch: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        torch = self.torch
        x = torch.from_numpy(np.ascontiguousarray(batch)).to(self.device)
        with torch.no_grad(), torch.autocast(device_type=self.device.type,
                                             dtype=torch.float16, enabled=self.fp16):
            out = self.module(x)
        if isinstance(out, (tuple, list)):
            boxes, logits = out[0], out[1]
        else:
            boxes, logits = out["pred_boxes"], out["pred_logits"]
        return boxes.float().cpu().numpy(), logits.float().cpu().numpy()


class OnnxBackend:
    """ONNX Runtime; CUDA provider if present, else CPU. Output names from rfdetr export."""

    def __init__(self, onnx_path: str | Path, providers: list[str] | None = None):
        import onnxruntime as ort
        avail = ort.get_available_providers()
        prefer = providers or ["CUDAExecutionProvider", "CPUExecutionProvider"]
        use = [p for p in prefer if p in avail] or ["CPUExecutionProvider"]
        self.session = ort.InferenceSession(str(onnx_path), providers=use)
        self.name = "onnxruntime-" + ("cuda" if use[0].startswith("CUDA") else "cpu")
        self.input_name = self.session.get_inputs()[0].name
        names = [o.name for o in self.session.get_outputs()]
        self.i_boxes = next((i for i, n in enumerate(names) if "dets" in n), 0)
        self.i_logits = next((i for i, n in enumerate(names) if "labels" in n), 1)
        shp = self.session.get_inputs()[0].shape
        self.resolution = int(shp[-1]) if isinstance(shp[-1], int) else None

    def forward(self, batch: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        out = self.session.run(None, {self.input_name: np.ascontiguousarray(batch, np.float32)})
        return np.asarray(out[self.i_boxes], np.float32), np.asarray(out[self.i_logits], np.float32)


class TensorRTBackend:  # pragma: no cover - needs an NVIDIA GPU and TensorRT
    """TensorRT 10 engine with torch-allocated device buffers.

    Uses the name-based tensor API (``set_tensor_address`` +
    ``execute_async_v3``). Validated on the workstation by ``ml/export.py``,
    which compares its outputs with the PyTorch model on real images; do the
    same on the aircraft after building the engine there, because engines are
    not portable across GPUs, TensorRT versions or JetPack releases.
    """
    name = "tensorrt"

    def __init__(self, engine_path: str | Path):
        import tensorrt as trt
        import torch
        self.trt, self.torch = trt, torch
        logger = trt.Logger(trt.Logger.WARNING)
        trt.init_libnvinfer_plugins(logger, "")
        with open(engine_path, "rb") as f, trt.Runtime(logger) as rt:
            self.engine = rt.deserialize_cuda_engine(f.read())
        if self.engine is None:
            raise RuntimeError(f"failed to deserialize {engine_path}: engines are not portable "
                               "across GPUs / TensorRT / JetPack versions -- rebuild on this device")
        self.context = self.engine.create_execution_context()
        self.names = [self.engine.get_tensor_name(i) for i in range(self.engine.num_io_tensors)]
        self.inputs = [n for n in self.names
                       if self.engine.get_tensor_mode(n) == trt.TensorIOMode.INPUT]
        self.outputs = [n for n in self.names if n not in self.inputs]
        self.buffers = {}
        for n in self.names:
            shape = tuple(self.engine.get_tensor_shape(n))
            if any(d < 0 for d in shape):
                raise NotImplementedError("dynamic shapes: export with a static shape")
            dtype = torch.from_numpy(np.empty(0, trt.nptype(self.engine.get_tensor_dtype(n)))).dtype
            self.buffers[n] = torch.empty(shape, dtype=dtype, device="cuda")
            self.context.set_tensor_address(n, self.buffers[n].data_ptr())
        self.stream = torch.cuda.Stream()
        self.resolution = int(self.buffers[self.inputs[0]].shape[-1])
        self.o_boxes = next((n for n in self.outputs if "dets" in n), self.outputs[0])
        self.o_logits = next((n for n in self.outputs if "labels" in n), self.outputs[-1])

    def forward(self, batch: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        torch = self.torch
        inp = self.buffers[self.inputs[0]]
        inp.copy_(torch.from_numpy(np.ascontiguousarray(batch)).to(inp.dtype))
        with torch.cuda.stream(self.stream):
            self.context.execute_async_v3(self.stream.cuda_stream)
        self.stream.synchronize()
        return (self.buffers[self.o_boxes].float().cpu().numpy(),
                self.buffers[self.o_logits].float().cpu().numpy())


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

def load_rfdetr(checkpoint: str | Path, size: str = "medium", resolution: int | None = None,
                num_classes: int | None = None):
    """Load a fine-tuned RF-DETR checkpoint with the rfdetr package (workstation only)."""
    import rfdetr
    cls = {"nano": rfdetr.RFDETRNano, "small": rfdetr.RFDETRSmall,
           "medium": rfdetr.RFDETRMedium, "large": rfdetr.RFDETRLarge}[size]
    kw = {"pretrain_weights": str(checkpoint)}
    if resolution:
        kw["resolution"] = int(resolution)
    if num_classes:
        kw["num_classes"] = int(num_classes)
    return cls(**kw)


class RFDETRDetector:
    """A ``fa_gateway.perception.Detector`` over any backend.

    ``classes`` are the dataset classes; ``self.classes`` appends ``background``
    so that ``Detection.probs`` indexes it directly.
    """

    def __init__(self, backend: Backend, classes: list[str], resolution: int,
                 threshold: float = 0.3, model_version: str = "rfdetr"):
        self.backend = backend
        self.object_classes = list(classes)
        self.classes = list(classes) + [BACKGROUND]
        self.resolution = int(resolution)
        self.threshold = float(threshold)
        self.model_version = f"{model_version}+{backend.name}"

    def queries(self, image: np.ndarray) -> DecodedQueries:
        h, w = np.asarray(image).shape[:2]
        boxes, logits = self.backend.forward(preprocess(image, self.resolution)[None])
        return decode_queries(boxes, logits, len(self.object_classes), w, h)

    def infer(self, image: np.ndarray):
        from .perception import Detection
        q = self.queries(image)
        keep = np.flatnonzero(q.score >= self.threshold)
        keep = keep[np.argsort(-q.score[keep])]
        return [Detection(tuple(map(float, q.boxes_xyxy[i])), q.probs[i], float(q.score[i]))
                for i in keep]

    def __call__(self, image: np.ndarray, frame_id: str = ""):
        import time
        from .perception import FrameResult
        t0 = time.perf_counter()
        dets = self.infer(image)
        return FrameResult(frame_id=frame_id or f"f{int(t0 * 1e6)}", detections=dets,
                           latency_ms=(time.perf_counter() - t0) * 1000.0,
                           model_version=self.model_version,
                           height=int(np.asarray(image).shape[0]),
                           width=int(np.asarray(image).shape[1]))


def detector_from_run(run_dir: str | Path, backend: str = "auto",
                      threshold: float | None = None) -> RFDETRDetector:
    """Build a detector from an ``ml/runs/<name>`` directory.

    ``run.json`` (written by ``ml/train.py``) supplies the classes, size and
    resolution; ``eval/metrics.json`` (from ``ml/evaluate.py``) supplies the
    operating threshold chosen on validation data.
    """
    import json
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    classes, res = meta["classes"], int(meta["resolution"])
    if threshold is None:
        m = run_dir / "eval" / "metrics.json"
        threshold = (json.loads(m.read_text(encoding="utf-8"))["operating_threshold"] if m.exists() else 0.3)
    tried = []
    order = {"auto": ["tensorrt", "onnx", "torch"]}.get(backend, [backend])
    for kind in order:
        try:
            if kind == "tensorrt":
                eng = sorted((run_dir / "export").glob("*.engine")) + sorted((run_dir / "export").glob("*.trt"))
                if not eng:
                    raise FileNotFoundError("no TensorRT engine in export/")
                be = TensorRTBackend(eng[0])
            elif kind == "onnx":
                onx = sorted((run_dir / "export").glob("*.onnx"))
                if not onx:
                    raise FileNotFoundError("no ONNX model in export/")
                be = OnnxBackend(onx[0])
            elif kind == "torch":
                be = TorchBackend(load_rfdetr(run_dir / meta.get("checkpoint", "checkpoint_best_total.pth"),
                                              meta["size"], res, len(classes)))
            else:
                raise ValueError(f"unknown backend {kind!r}")
            return RFDETRDetector(be, classes, res, threshold,
                                  model_version=meta.get("model_version", "rfdetr"))
        except Exception as e:  # noqa: BLE001
            tried.append(f"{kind}: {type(e).__name__}: {e}")
            log.info("backend %s unavailable: %s", kind, e)
    raise RuntimeError("no RF-DETR backend could load: " + "; ".join(tried))
