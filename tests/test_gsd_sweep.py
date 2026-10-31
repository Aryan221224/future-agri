"""The GSD-sweep canvas builder scales and offsets ground truth with the image."""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ml" / "h200"))
from gsd_sweep import canvases  # noqa: E402


def test_canvas_k2_scales_and_offsets(tmp_path):
    ims, anns = [], []
    for i in range(4):
        Image.fromarray(np.full((600, 600, 3), 40 * i, np.uint8)).save(tmp_path / f"{i}.png")
        ims.append({"id": i + 1, "file_name": f"{i}.png", "width": 600, "height": 600})
        anns.append({"image_id": i + 1, "category_id": 1, "bbox": [100, 200, 40, 60]})
    coco = {"images": ims, "annotations": anns, "categories": [{"id": 1, "name": "maize"}]}
    out = list(canvases(coco, tmp_path, 2))
    assert len(out) == 1
    arr, boxes, px = out[0]
    assert arr.shape == (600, 600, 3) and px == 360000 and len(boxes) == 4
    assert boxes[0][:4] == (50.0, 100.0, 70.0, 130.0)          # tile (0,0): /2
    assert boxes[3][:4] == (350.0, 400.0, 370.0, 430.0)        # tile (1,1): /2 + 300
