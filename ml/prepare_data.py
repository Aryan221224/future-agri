#!/usr/bin/env python3
"""Download WeedsGalore (CC BY 4.0, 321 MB) and convert it to COCO for RF-DETR.

    python ml/prepare_data.py                      # -> ../data/weedsgalore_coco
    python ml/prepare_data.py --zip C:\\path\\weedsgalore-dataset.zip
    python ml/prepare_data.py --synthetic          # tiny fake set for a smoke run

Output: ``train/ valid/ test/`` with ``_annotations.coco.json``, ``meta.json``
(counts per split and date, licence, citation) and ``preview_train.jpg`` --
open the preview and check that boxes sit on plants before training.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fa_ml import DATA_DIR  # noqa: E402
from fa_ml import synthetic, weedsgalore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", type=Path, default=DATA_DIR)
    ap.add_argument("--zip", type=Path, help="use an already-downloaded zip")
    ap.add_argument("--out", type=Path, help="COCO output dir (default <data-root>/weedsgalore_coco)")
    ap.add_argument("--min-area", type=int, default=16, help="drop instances smaller than this (px)")
    ap.add_argument("--no-balance", action="store_true", help="no per-channel gain on the RGB composite")
    ap.add_argument("--synthetic", action="store_true", help="fake data for a smoke run (no download)")
    a = ap.parse_args()

    if a.synthetic:
        raw = synthetic.make(a.data_root / "weedsgalore_synthetic_raw")
        out = a.out or a.data_root / "weedsgalore_synthetic_coco"
    else:
        zp = a.zip or weedsgalore.download(a.data_root / "downloads")
        raw = weedsgalore.extract(zp, a.data_root / "weedsgalore_raw")
        out = a.out or a.data_root / "weedsgalore_coco"
    print(f"raw dataset: {raw}")
    meta = weedsgalore.convert(raw, out, min_area=a.min_area, balance=not a.no_balance)
    if a.synthetic:
        meta["synthetic"] = True
        (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"COCO dataset: {out}")
    print(f"check the boxes: {out / 'preview_train.jpg'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
