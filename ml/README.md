# ml/ — training the real perception model

The gateway's detector was a deterministic stub. This folder trains the real
one on the lab PC's RTX A6000, calibrates its conformal gate on real plants,
and exports it for the edge.

| file | does |
|---|---|
| `prepare_data.py` | downloads **WeedsGalore** (WACV 2025, CC BY 4.0, 321 MB) and converts it to COCO |
| `train.py` | fine-tunes **RF-DETR** (Apache-2.0 code and weights; Nano–Large only) |
| `evaluate.py` | COCO mAP, then the conformal gate: coverage, set size, review rate, per-date strata, date-shift test |
| `export.py` | ONNX (+ TensorRT FP16), parity against PyTorch, latency |
| `run_pipeline.ps1` | all four, logged, on Windows |
| `fa_ml/` | dataset conversion, matching, detection conformal, provenance |

Runtime code (preprocessing, decoding, the TensorRT / ONNX / PyTorch backends)
is in `services/gateway/fa_gateway/rfdetr.py`, so calibration and the aircraft
run the same path. See [ADR 0012](../docs/adr/0012-detection-conformal.md).

## Run it (lab PC, PowerShell)

1. `cd C:\Users\aryan\Documents\FutureAgri\futureagri`
2. `powershell -ExecutionPolicy Bypass -File ml\run_pipeline.ps1`
3. Results: `ml\results\wg-medium\` — `report.md`, `MODEL_CARD.md`, `coverage.png`,
   `shift.png`, `latency.json`, `calibration.json`. Commit that folder; `ml\runs\`
   (checkpoints, caches) stays local.

Options: `-Size small` (faster), `-Trt` (TensorRT engine; install with
`setup_pc.ps1 -WithTensorRT` first), `-SkipData` / `-SkipTrain` to resume.

## Use the trained model in the gateway

```python
from fa_core.uncertainty import StratifiedCalibrator
from fa_gateway.perception import build_detector
from fa_gateway.pipeline import EdgePipeline

det = build_detector("auto", run_dir="ml/runs/wg-medium")      # TensorRT > ONNX > PyTorch
cal = StratifiedCalibrator.from_json("ml/results/wg-medium/calibration.json")
pipe = EdgePipeline(det, cal, det.classes, crop="maize", stage="2023-06-06", site="weedsgalore-de")
```

## Smoke test without a GPU or the data

```bash
python ml/prepare_data.py --synthetic
python ml/train.py --smoke --size nano --resolution 384 --device cpu --data ../data/weedsgalore_synthetic_coco
python ml/evaluate.py --run ml/runs/smoke-nano --min-stratum 5
python ml/export.py --run ml/runs/smoke-nano --images 3 --iters 5
```

This checks the plumbing only: a 1-epoch randomly initialised model detects nothing.
