"""A tiny fake WeedsGalore in the official on-disk layout.

For tests and for a CPU smoke run of the whole training -> evaluation -> export
chain on a machine without the real data or a GPU. Plants are coloured
ellipses on soil-coloured noise; every image says SYNTHETIC in its id.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

DATES = ["2023-05-25", "2023-05-30", "2023-06-06", "2023-06-15"]
# Rough per-class colour (R, G, B) in [0,1] so a detector can learn something.
COLOURS = {1: (0.20, 0.55, 0.15), 2: (0.45, 0.30, 0.25), 3: (0.55, 0.70, 0.20),
           4: (0.25, 0.35, 0.60), 5: (0.70, 0.65, 0.30)}


def make(root: Path, n=(8, 4, 4), size: int = 96, seed: int = 0, per_image=(3, 7),
         instances: bool = True) -> Path:
    rng = np.random.default_rng(seed)
    root = Path(root)
    (root / "splits").mkdir(parents=True, exist_ok=True)
    k = 0
    for split, count in zip(("train", "val", "test"), n):
        ids = []
        for i in range(count):
            date = DATES[(i + k) % len(DATES)]
            sid = f"{date}_{k:04d}"
            k += 1
            ids.append(sid)
            for sub in ("images", "semantics", "instances"):
                (root / date / sub).mkdir(parents=True, exist_ok=True)
            rgb = np.stack([np.full((size, size), c) for c in (0.35, 0.28, 0.20)], -1)
            rgb = rgb + rng.normal(0, 0.03, rgb.shape)
            sem = np.zeros((size, size), np.uint8)
            inst = np.zeros((size, size), np.uint16)
            yy, xx = np.mgrid[:size, :size]
            for j in range(int(rng.integers(*per_image))):
                c = int(rng.integers(1, 6))
                cy, cx = rng.uniform(8, size - 8, 2)
                ry, rx = rng.uniform(3, 9, 2)
                m = ((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2 <= 1
                rgb[m] = COLOURS[c]
                sem[m] = c
                inst[m] = j + 1
            rgb = np.clip(rgb, 0, 1)
            base = root / date / "images" / sid
            for b, ch in (("R", 0), ("G", 1), ("B", 2)):
                Image.fromarray((rgb[..., ch] * 255).astype(np.uint8)).save(f"{base}_{b}.png")
            for b in ("NIR", "RE"):
                Image.fromarray((rgb[..., 1] * 200).astype(np.uint8)).save(f"{base}_{b}.png")
            Image.fromarray(sem).save(root / date / "semantics" / f"{sid}.png")
            if instances:
                Image.fromarray(inst).save(root / date / "instances" / f"{sid}.png")
        (root / "splits" / f"{split}.txt").write_text("\n".join(ids) + "\n", encoding="utf-8")
    return root
