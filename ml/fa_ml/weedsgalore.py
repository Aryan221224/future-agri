"""WeedsGalore (WACV 2025): download and convert to COCO for RF-DETR.

Dataset
-------
Celikkan et al., "WeedsGalore: A Multispectral and Multitemporal UAV-Based
Dataset for Crop and Weed Segmentation in Agricultural Maize Fields", WACV 2025.
156 patches of 600 x 600 px at 2.5 mm GSD (DJI P4 Multispectral, 5 m AGL),
four dates in May-June 2023, 104 / 26 / 26 train / val / test, instance masks
for ~12 000 plants: maize, amaranth, barnyard grass, quickweed, other weeds.
Licence: **CC BY 4.0** -- allowed by ADR 0009 (ship-model training data must be
CC BY or CC0). Attribution is written into every COCO file and the model card.

Layout of the official zip (from the authors' loader)::

    <root>/<YYYY-MM-DD>/images/<id>_{R,G,B,NIR,RE}.png
    <root>/<YYYY-MM-DD>/semantics/<id>.png     0 bg, 1 maize, 2..5 weeds
    <root>/<YYYY-MM-DD>/instances/<id>.png     one id per plant
    <root>/splits/{train,val,test}.txt          ids; first 10 chars are the date

Conversion writes ``train/ valid/ test/`` with ``_annotations.coco.json`` (the
layout RF-DETR detects automatically), RGB composites from the R, G and B
bands, and each image's acquisition date -- the growth-stage stratum the
conformal gate is calibrated per.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

URL = "https://doidata.gfz-potsdam.de/weedsgalore_e_celikkan_2024/weedsgalore-dataset.zip"
LANDING = "https://doidata.gfz.de/weedsgalore_e_celikkan_2024/"
CLASSES = ["maize", "amaranth", "barnyard_grass", "quickweed", "weed_other"]   # semantic ids 1..5
# How the native classes map onto the Future Agri gateway taxonomy.
FA_TAXONOMY = {"maize": "crop", "amaranth": "broadleaf_weed", "quickweed": "broadleaf_weed",
               "barnyard_grass": "grass_weed", "weed_other": "weed_other"}
SPLITS = {"train": "train", "val": "valid", "test": "test"}     # official -> RF-DETR folder
CITATION = ("Celikkan E., Kunzmann T., Yeskaliyev Y., Itzerott S., Klein N., Herold M. "
            "WeedsGalore: A Multispectral and Multitemporal UAV-Based Dataset for Crop and "
            "Weed Segmentation in Agricultural Maize Fields. WACV 2025, pp. 4767-4777.")
LICENCE = {"id": 1, "name": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download(dest_dir: Path, url: str = URL, chunk: int = 1 << 20) -> Path:
    """Resumable download. Re-running continues a partial file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / url.rsplit("/", 1)[-1]
    part = out.with_suffix(out.suffix + ".part")
    if out.exists():
        print(f"already downloaded: {out} ({out.stat().st_size / 1e6:.0f} MB)")
        return out
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "futureagri/1.0",
                                               **({"Range": f"bytes={have}-"} if have else {})})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"download failed ({type(e).__name__}: {e}).\n"
                 f"Download it in a browser from {LANDING} and pass --zip <path>.")
    total = int(resp.headers.get("Content-Length") or 0) + (have if resp.status == 206 else 0)
    if resp.status != 206:
        have = 0
    mode = "ab" if have else "wb"
    t0, last = time.time(), -1
    with open(part, mode) as fh:            # binary: mode is "wb" or "ab"
        done = have
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            fh.write(buf)
            done += len(buf)
            if total:
                pct = int(100 * done / total)
                if pct != last and pct % 5 == 0:
                    rate = (done - have) / max(time.time() - t0, 1e-3) / 1e6
                    print(f"  {pct:3d}%  {done / 1e6:7.1f} / {total / 1e6:.1f} MB  {rate:.1f} MB/s")
                    last = pct
    part.replace(out)
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def extract(zip_path: Path, dest_dir: Path) -> Path:
    marker = dest_dir / ".extracted"
    if not marker.exists():
        print(f"extracting {zip_path.name} ...")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(dest_dir)
        marker.write_text(zip_path.name, encoding="utf-8")
    return find_root(dest_dir)


def find_root(base: Path) -> Path:
    hits = sorted(p.parent.parent for p in Path(base).rglob("splits/train.txt"))
    if not hits:
        raise FileNotFoundError(f"no splits/train.txt under {base}: is this the WeedsGalore zip?")
    return hits[0]


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def load_band(path: Path) -> np.ndarray:
    """One band as float32 in [0, 1], whatever bit depth the PNG uses."""
    im = Image.open(path)
    a = np.asarray(im)
    if a.ndim == 3:
        a = a[..., 0]
    if a.dtype == np.uint8:
        return a.astype(np.float32) / 255.0
    if a.dtype in (np.uint16, np.int32, np.int16):
        return np.clip(a.astype(np.float32) / 65535.0, 0, 1)
    a = a.astype(np.float32)
    return np.clip(a / (a.max() if a.max() > 1 else 1.0), 0, 1)


def rgb_composite(root: Path, sid: str, gain: np.ndarray | None = None) -> np.ndarray:
    base = root / sid[:10] / "images" / sid
    rgb = np.stack([load_band(Path(f"{base}_{b}.png")) for b in ("R", "G", "B")], axis=-1)
    if gain is not None:
        rgb = rgb * gain[None, None, :]
    return (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)


def _instance_ids(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim == 3:
        a = a[..., :3].astype(np.int64)
        return (a[..., 0] << 16) | (a[..., 1] << 8) | a[..., 2]
    return a.astype(np.int64)


def instances_from_masks(inst: np.ndarray | None, sem: np.ndarray, min_area: int = 16
                         ) -> list[tuple[int, list[float], int]]:
    """[(category_id 1..5, [x, y, w, h], area)] for one patch.

    Class of an instance = majority semantic label over its pixels. If the patch
    has no instance mask, fall back to 8-connected components of each class.
    """
    sem = np.asarray(sem)
    if sem.ndim == 3:
        sem = sem[..., 0]
    out = []
    if inst is None:
        from scipy import ndimage
        for c in range(1, len(CLASSES) + 1):
            lab, n = ndimage.label(sem == c, structure=np.ones((3, 3), int))
            for i, sl in enumerate(ndimage.find_objects(lab), start=1):
                if sl is None:
                    continue
                area = int((lab[sl] == i).sum())
                if area >= min_area:
                    y, x = sl
                    out.append((c, [float(x.start), float(y.start),
                                    float(x.stop - x.start), float(y.stop - y.start)], area))
        return out
    ids = _instance_ids(inst)
    from scipy import ndimage
    uniq = np.unique(ids)
    uniq = uniq[uniq != 0]
    if uniq.size == 0:
        return out
    # Relabel to 1..n so find_objects is one pass over the patch.
    rel = np.where(ids > 0, np.searchsorted(uniq, ids) + 1, 0).astype(np.int32)
    for i, sl in enumerate(ndimage.find_objects(rel), start=1):
        if sl is None:
            continue
        m = rel[sl] == i
        area = int(m.sum())
        if area < min_area:
            continue
        cls = sem[sl][m]
        cls = cls[(cls > 0) & (cls <= len(CLASSES))]
        if cls.size == 0:
            continue
        c = int(np.bincount(cls).argmax())
        ys, xs = np.nonzero(m)
        y0, x0 = sl[0].start + ys.min(), sl[1].start + xs.min()
        y1, x1 = sl[0].start + ys.max() + 1, sl[1].start + xs.max() + 1
        out.append((c, [float(x0), float(y0), float(x1 - x0), float(y1 - y0)], area))
    return out


def _read_split(root: Path, name: str) -> list[str]:
    f = root / "splits" / f"{name}.txt"
    return [s.strip() for s in f.read_text(encoding="utf-8").splitlines() if s.strip()]


def white_balance_gain(root: Path, ids: list[str], pct: float = 99.0) -> np.ndarray:
    """Per-channel gain mapping the train set's pct-th percentile to 0.9.

    Monochrome multispectral bands are often dark and unbalanced as an RGB
    composite. One gain per channel, fitted on *train only*, applied to every
    split -- so it is a fixed sensor correction, not per-image normalisation
    that would leak information or change with scene content.
    """
    vals = [[], [], []]
    for sid in ids[: min(len(ids), 40)]:
        base = root / sid[:10] / "images" / sid
        for k, b in enumerate(("R", "G", "B")):
            vals[k].append(np.percentile(load_band(Path(f"{base}_{b}.png")), pct))
    p = np.array([np.median(v) for v in vals], np.float32)
    return (0.9 / np.clip(p, 1e-3, None)).astype(np.float32)


def convert(root: Path, out_dir: Path, min_area: int = 16, balance: bool = True) -> dict:
    root, out_dir = Path(root), Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    train_ids = _read_split(root, "train")
    gain = white_balance_gain(root, train_ids) if balance else None
    meta = {"dataset": "WeedsGalore", "citation": CITATION, "licence": LICENCE["name"],
            "source": LANDING, "classes": CLASSES, "fa_taxonomy": FA_TAXONOMY,
            "rgb_gain": None if gain is None else [round(float(g), 4) for g in gain],
            "min_area_px": min_area, "splits": {}}
    categories = [{"id": i + 1, "name": c, "supercategory": "plant"} for i, c in enumerate(CLASSES)]
    for official, folder in SPLITS.items():
        ids = _read_split(root, official)
        d = out_dir / folder
        d.mkdir(parents=True)
        images, anns = [], []
        per_date: dict[str, Counter] = defaultdict(Counter)
        for n, sid in enumerate(ids, start=1):
            date = sid[:10]
            rgb = rgb_composite(root, sid, gain)
            fn = f"{sid}.png"
            Image.fromarray(rgb).save(d / fn)
            h, w = rgb.shape[:2]
            images.append({"id": n, "file_name": fn, "width": w, "height": h,
                           "date": date, "license": 1})
            sem = np.asarray(Image.open(root / date / "semantics" / f"{sid}.png"))
            ip = root / date / "instances" / f"{sid}.png"
            inst = np.asarray(Image.open(ip)) if ip.exists() else None
            for c, bbox, area in instances_from_masks(inst, sem, min_area):
                anns.append({"id": len(anns) + 1, "image_id": n, "category_id": c, "bbox": bbox,
                             "area": float(area), "iscrowd": 0, "segmentation": []})
                per_date[date][CLASSES[c - 1]] += 1
        coco = {"info": {"description": "WeedsGalore RGB composites (converted by Future Agri)",
                         "contributor": CITATION, "url": LANDING, "version": "1"},
                "licenses": [LICENCE], "categories": categories, "images": images, "annotations": anns}
        (d / "_annotations.coco.json").write_text(json.dumps(coco), encoding="utf-8")
        meta["splits"][folder] = {"images": len(images), "annotations": len(anns),
                                  "by_date": {k: dict(v) for k, v in sorted(per_date.items())}}
        print(f"  {folder:5s}: {len(images):4d} images  {len(anns):6d} plants")
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    preview(out_dir / "train", out_dir / "preview_train.jpg")
    return meta


def preview(split_dir: Path, out: Path, n: int = 4) -> None:
    """A 2x2 contact sheet with boxes, to check the conversion by eye."""
    from PIL import ImageDraw
    coco = json.loads((split_dir / "_annotations.coco.json").read_text(encoding="utf-8"))
    colours = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
    tiles = []
    for im in coco["images"][:n]:
        img = Image.open(split_dir / im["file_name"]).convert("RGB")
        dr = ImageDraw.Draw(img)
        for a in coco["annotations"]:
            if a["image_id"] == im["id"]:
                x, y, w, h = a["bbox"]
                dr.rectangle([x, y, x + w, y + h], outline=colours[(a["category_id"] - 1) % 5], width=2)
        tiles.append(img)
    if not tiles:
        return
    w, h = tiles[0].size
    sheet = Image.new("RGB", (w * 2, h * ((len(tiles) + 1) // 2)), "white")
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % 2) * w, (i // 2) * h))
    sheet.save(out, quality=90)
