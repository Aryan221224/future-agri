"""Provenance: what produced a number. Written into every run and result file."""
from __future__ import annotations

import platform
import subprocess
from datetime import datetime, timezone

from . import ROOT


def git_commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                           capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return (r.stdout.strip() or "unknown") + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def describe() -> dict:
    d = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "python": platform.python_version(), "os": f"{platform.system()} {platform.release()}",
         "machine": platform.node(), "git": git_commit()}
    try:
        import torch
        d["torch"] = torch.__version__
        d["cuda"] = torch.version.cuda
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            d["gpu"] = p.name
            d["gpu_mem_gb"] = round(p.total_memory / 2**30, 1)
        else:
            d["gpu"] = None
    except Exception:  # noqa: BLE001
        d["torch"] = None
    from importlib.metadata import PackageNotFoundError, version
    for mod, dist in (("rfdetr", "rfdetr"), ("onnxruntime", None), ("tensorrt", "tensorrt")):
        v = None
        for name in filter(None, (dist, mod, "onnxruntime-gpu" if mod == "onnxruntime" else None)):
            try:
                v = version(name)
                break
            except PackageNotFoundError:
                continue
        d[mod] = v
    return d
