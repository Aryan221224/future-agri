#!/usr/bin/env python3
"""Export a trained detector to ONNX (and TensorRT), check parity, measure latency.

    python ml/export.py --run ml/runs/wg-medium            # ONNX + benchmark
    python ml/export.py --run ml/runs/wg-medium --trt      # + TensorRT FP16 engine

Parity is checked on real validation images, per query, against the PyTorch
FP32 model: max |delta p| of the class-plus-background distribution the gate
uses, box error in pixels, and whether the kept detections (score >= the
operating threshold) agree. A backend that changes the gate's inputs changes
its coverage, so parity is reported next to speed, never instead of it.

The TensorRT engine is built for *this* GPU. It shows what TensorRT buys on an
RTX A6000; it says nothing about a Jetson. Build and re-check the engine on the
aircraft's board (same command, same parity test) before trusting it there.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fa_ml import ROOT  # noqa: E402
from fa_ml.env import describe  # noqa: E402
from fa_ml.matching import iou_matrix  # noqa: E402


def _images(data: Path, n: int) -> list[np.ndarray]:
    from PIL import Image
    coco = json.loads((data / "valid" / "_annotations.coco.json").read_text(encoding="utf-8"))
    return [np.asarray(Image.open(data / "valid" / im["file_name"]).convert("RGB"))
            for im in coco["images"][:n]]


def _time(fn, iters: int, warmup: int, cuda_sync) -> dict:
    for _ in range(warmup):
        fn()
    cuda_sync()
    ts = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        cuda_sync()
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    return {"median_ms": round(statistics.median(ts), 3), "p90_ms": round(ts[int(0.9 * (len(ts) - 1))], 3),
            "fps": round(1000 / statistics.median(ts), 1), "iters": iters}


def _parity(ref, other, thr: float) -> dict:
    """ref/other: lists of DecodedQueries for the same images."""
    dp, db, agree, n_kept = [], [], 0, 0
    for a, b in zip(ref, other):
        dp.append(float(np.abs(a.probs - b.probs).max()))
        db.append(float(np.abs(a.boxes_xyxy - b.boxes_xyxy).max()))
        ka, kb = np.flatnonzero(a.score >= thr), np.flatnonzero(b.score >= thr)
        n_kept += len(ka)
        same = len(ka) == len(kb)
        if same and len(ka):
            iou = iou_matrix(a.boxes_xyxy[ka], b.boxes_xyxy[kb])
            j = iou.argmax(1)
            same = bool((iou.max(1) > 0.9).all() and
                        (a.probs[ka].argmax(1) == b.probs[kb][j].argmax(1)).all())
        agree += int(same)
    return {"max_abs_dprob": round(max(dp), 5), "mean_max_abs_dprob": round(float(np.mean(dp)), 5),
            "max_box_err_px": round(max(db), 3), "images_with_identical_decisions": f"{agree}/{len(ref)}",
            "kept_detections_ref": n_kept}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--trt", action="store_true", help="also build a TensorRT FP16 engine (needs tensorrt)")
    ap.add_argument("--images", type=int, default=20, help="validation images for parity")
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=30)
    ap.add_argument("--skip-export", action="store_true", help="benchmark existing files only")
    a = ap.parse_args()

    run = json.loads((a.run / "run.json").read_text(encoding="utf-8"))
    res, classes = int(run["resolution"]), run["classes"]
    m = a.run / "eval" / "metrics.json"
    thr = json.loads(m.read_text(encoding="utf-8"))["operating_threshold"] if m.exists() else 0.3
    exp = a.run / "export"
    exp.mkdir(exist_ok=True)

    from fa_gateway.rfdetr import (OnnxBackend, RFDETRDetector, TensorRTBackend, TorchBackend,
                                   load_rfdetr, preprocess)
    import torch
    rf = load_rfdetr(a.run / run["checkpoint"], run["size"], res, len(classes))
    cuda = torch.cuda.is_available()
    sync = torch.cuda.synchronize if cuda else (lambda: None)

    if not a.skip_export:
        print("exporting ONNX ...")
        p = rf.export(output_dir=str(exp), format="onnx", shape=(res, res), batch_size=1,
                      opset_version=17, verbose=False, notes={"futureagri_run": run["name"]})
        print(f"  {p}")
        if a.trt:
            print("building TensorRT FP16 engine (a few minutes) ...")
            try:
                p = rf.export(output_dir=str(exp), format="tensorrt", shape=(res, res), batch_size=1,
                              fp16=True, verbose=False)
                print(f"  {p}")
            except Exception as e:  # noqa: BLE001
                print(f"  TensorRT build failed: {type(e).__name__}: {e}")
        # rfdetr cleared model.model? reload to be safe for the torch reference
        rf = load_rfdetr(a.run / run["checkpoint"], run["size"], res, len(classes))

    imgs = _images(Path(run["dataset"]), a.images)
    batch = preprocess(imgs[0], res)[None]
    backends = {"torch-fp32": lambda: TorchBackend(rf)}
    if cuda:
        backends["torch-fp16"] = lambda: TorchBackend(rf, fp16=True)
    onx = sorted(exp.glob("*.onnx"))
    if onx:
        backends["onnx"] = lambda: OnnxBackend(onx[0])
    eng = sorted(exp.glob("*.trt")) + sorted(exp.glob("*.engine"))
    if eng:
        backends["tensorrt"] = lambda: TensorRTBackend(eng[0])

    results, ref = {}, None
    for name, make in backends.items():
        try:
            be = make()
        except Exception as e:  # noqa: BLE001
            results[name] = {"error": f"{type(e).__name__}: {e}"}
            print(f"  {name:12s} unavailable: {e}")
            continue
        det = RFDETRDetector(be, classes, res, thr)
        qs = [det.queries(im) for im in imgs]
        r = {"backend": be.name,
             "forward": _time(lambda: be.forward(batch), a.iters, a.warmup, sync),
             "end_to_end": _time(lambda: det.infer(imgs[0]), max(a.iters // 4, 10), 5, sync)}
        if ref is None:
            ref = qs
            r["parity"] = "reference"
        else:
            r["parity"] = _parity(ref, qs, thr)
        results[name] = r
        print(f"  {name:12s} forward {r['forward']['median_ms']:8.2f} ms  "
              f"({r['forward']['fps']} fps)  end-to-end {r['end_to_end']['median_ms']:.1f} ms  "
              f"parity {r['parity'] if isinstance(r['parity'], str) else r['parity']['max_abs_dprob']}")

    out = {"run": run["name"], "model_version": run["model_version"], "resolution": res,
           "operating_threshold": thr, "batch": 1, "results": results, "env": describe(),
           "note": ("forward = model only on a preprocessed tensor; end_to_end = numpy preprocessing "
                    "+ forward + decoding on one 600x600 frame. The TensorRT engine is specific to this "
                    "GPU and TensorRT version; Jetson numbers must be measured on the Jetson.")}
    (exp / "bench.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    pub = ROOT / "ml" / "results" / run["name"]
    pub.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exp / "bench.json", pub / "latency.json")
    print(f"\n-> {exp / 'bench.json'}\n-> {pub / 'latency.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
