"""The GSD-sweep canvas builder scales and offsets ground truth with the image, keeps
every test image (a partial last canvas), and an empty factor does not crash the run."""
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


def _coco(tmp_path, n=5, size=600):
    from PIL import Image
    ims, anns = [], []
    for i in range(n):
        Image.fromarray(np.full((size, size, 3), 40 * i, np.uint8)).save(tmp_path / f"t{i}.png")
        ims.append({"id": i, "file_name": f"t{i}.png", "width": size, "height": size})
        anns.append({"id": i, "image_id": i, "category_id": 1, "bbox": [100, 100, 40, 40], "area": 1600})
    return {"images": ims, "annotations": anns, "categories": [{"id": 1, "name": "weed_other"}]}


def test_partial_last_canvas_keeps_every_image(tmp_path):
    import gsd_sweep
    coco = _coco(tmp_path)
    out = list(gsd_sweep.canvases(coco, tmp_path, k=2))
    assert len(out) == 2                                   # 4 tiles + a partial canvas of 1
    arr, boxes, px = out[1]
    assert arr.shape == (600, 600, 3) and len(boxes) == 1 and px == 300 * 300
    x1, y1, x2, y2, _ = boxes[0]
    assert (x1, y1, x2, y2) == (50, 50, 70, 70)            # scaled by 1/k in the first slot
    assert sum(len(b) for _, b, _ in gsd_sweep.canvases(coco, tmp_path, k=8)) == 5   # one canvas, all images


def test_fmt_handles_missing_values():
    import gsd_sweep
    assert gsd_sweep.fmt(None) == "n/a" and gsd_sweep.fmt(0.12345) == "0.123"
