#!/usr/bin/env python3
"""Fine-tune RF-DETR (Apache-2.0 code and weights) on a COCO dataset.

    python ml/train.py --data ..\\data\\weedsgalore_coco --size medium --name wg-medium
    python ml/train.py --data ... --size small --epochs 150 --batch-size 8
    python ml/train.py --smoke --data <synthetic coco>      # 1 epoch on CPU, random init

Writes ``ml/runs/<name>/``:
  run.json                      what was trained, on what, where (read by evaluate/export
                                and by fa_gateway.rfdetr.detector_from_run)
  checkpoint_best_total.pth     the model to evaluate and ship
  ...                           rfdetr's own logs and periodic checkpoints

Only Nano, Small, Medium and Large are offered: their weights are Apache-2.0.
XLarge / 2XLarge weights are under Roboflow's PML 1.0 and are excluded by
ADR 0009.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fa_ml import RUNS_DIR  # noqa: E402
from fa_ml.env import describe  # noqa: E402

SIZES = ("nano", "small", "medium", "large")
DIVISOR = 32          # patch 16 x 2 windows for every offered size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True, help="COCO dir with train/ valid/ test/")
    ap.add_argument("--size", choices=SIZES, default="medium")
    ap.add_argument("--name", help="run name (default wg-<size>)")
    ap.add_argument("--resolution", type=int, default=608,
                    help="square input size, multiple of 32 (608 ~ native 600 px patches)")
    ap.add_argument("--epochs", type=int, default=150)
    ap.add_argument("--batch-size", default="auto", help='int, or "auto" to probe GPU memory')
    ap.add_argument("--grad-accum", type=int, default=1)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--patience", type=int, default=25, help="early-stopping patience (epochs)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default=None, help="cuda | cpu (default: cuda if available)")
    ap.add_argument("--resume", type=Path, help="checkpoint to resume from (last.ckpt)")
    ap.add_argument("--smoke", action="store_true",
                    help="1 epoch, CPU, no pretrained weights: checks the pipeline, not the model")
    a = ap.parse_args()

    if a.resolution % DIVISOR:
        sys.exit(f"--resolution must be a multiple of {DIVISOR}")
    import torch
    device = a.device or ("cuda" if torch.cuda.is_available() else "cpu")
    if device == "cpu" and not a.smoke:
        sys.exit("No CUDA GPU visible to PyTorch. On the lab PC run:\n"
                 "  python -c \"import torch; print(torch.cuda.is_available(), torch.version.cuda)\"\n"
                 "If False, reinstall torch from the CUDA index (see ml/README.md, step 2).\n"
                 "Use --device cpu --smoke only to test the pipeline.")

    coco = json.loads((a.data / "train" / "_annotations.coco.json").read_text(encoding="utf-8"))
    classes = [c["name"] for c in sorted(coco["categories"], key=lambda c: c["id"])]
    meta_path = a.data / "meta.json"
    data_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    name = a.name or (f"smoke-{a.size}" if a.smoke else f"wg-{a.size}")
    out = RUNS_DIR / name
    out.mkdir(parents=True, exist_ok=True)

    torch.manual_seed(a.seed)
    import rfdetr
    cls = {"nano": rfdetr.RFDETRNano, "small": rfdetr.RFDETRSmall,
           "medium": rfdetr.RFDETRMedium, "large": rfdetr.RFDETRLarge}[a.size]
    model = cls(pretrain_weights=None) if a.smoke else cls()   # COCO-pretrained, Apache-2.0

    run = {"name": name, "size": a.size, "resolution": a.resolution, "classes": classes,
           "dataset": str(a.data.resolve()), "dataset_meta": {k: data_meta.get(k) for k in
                                                              ("dataset", "licence", "citation",
                                                               "rgb_gain", "synthetic", "splits")},
           "checkpoint": "checkpoint_best_total.pth", "smoke": a.smoke,
           "model_version": f"rfdetr-{a.size}-{name}", "args": {k: str(v) for k, v in vars(a).items()},
           "env": describe(), "status": "training"}
    (out / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    print(f"training RF-DETR-{a.size} at {a.resolution}px on {device} "
          f"({run['env'].get('gpu') or 'CPU'}) -> {out}")

    bs: int | str = a.batch_size if a.batch_size == "auto" else int(a.batch_size)
    kw = dict(dataset_dir=str(a.data), output_dir=str(out), epochs=1 if a.smoke else a.epochs,
              batch_size=2 if a.smoke else bs, grad_accum_steps=a.grad_accum, lr=a.lr,
              num_workers=0 if a.smoke else a.workers, resolution=a.resolution, device=device,
              early_stopping=not a.smoke, early_stopping_patience=a.patience,
              notes={"futureagri_run": name, "git": run["env"]["git"],
                     "dataset": run["dataset_meta"].get("dataset"),
                     "licence": run["dataset_meta"].get("licence")})
    if a.smoke:
        kw.update(multi_scale=False, run_test=False, use_ema=False)
    if a.resume:
        kw["resume"] = str(a.resume)
    t0 = time.time()
    try:
        model.train(**kw)
    except Exception as e:  # noqa: BLE001
        run.update(status="failed", error=f"{type(e).__name__}: {e}")
        (out / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
        raise
    ckpt = out / "checkpoint_best_total.pth"
    if not ckpt.exists():
        cands = sorted(out.glob("checkpoint_best*.pth")) + sorted(out.glob("*.pth"))
        if cands:
            run["checkpoint"] = cands[0].name
    run.update(status="trained", train_minutes=round((time.time() - t0) / 60, 1),
               finished_env=describe())
    (out / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    print(f"\ndone in {run['train_minutes']} min -> {out / run['checkpoint']}")
    print(f"next: python ml/evaluate.py --run {out}")
    return 0


if __name__ == "__main__":      # required on Windows: data-loader workers are spawned
    raise SystemExit(main())
