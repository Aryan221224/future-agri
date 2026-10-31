"""Future Agri perception training, calibration and export (workstation side).

The runtime half -- preprocessing, decoding and the TensorRT / ONNX / PyTorch
backends -- lives in ``services/gateway/fa_gateway/rfdetr.py`` so that the
aircraft and the calibration job run identical code. This package only adds
what a workstation needs: datasets, training, evaluation and export.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in ("packages/fa_core", "services/gateway", "services/twin", "services/planner", "sim", "ml"):
    _s = str(ROOT / _p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

# Datasets live outside the repo (Documents/FutureAgri/data on the lab PC); FA_DATA overrides.
DATA_DIR = Path(os.environ.get("FA_DATA", ROOT.parent / "data"))
RUNS_DIR = ROOT / "ml" / "runs"
