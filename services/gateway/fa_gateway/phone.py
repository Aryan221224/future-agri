"""Ingest phone photographs from a field visit.

A phone is a legitimate instrument for this system, with one caveat that decides
everything: the processing pipeline inside a phone camera exists to make images
look good, and every one of those steps -- auto exposure, auto white balance,
tone mapping, local contrast, denoise, sharpening -- is a measurement error.

So this module does two things. It extracts what can be trusted (geometry,
colour ratios, texture) and it *checks* the things that must have been held
fixed, refusing to silently ingest a series that was shot on auto. Measured
consequence of getting that wrong: a published nitrogen retrieval fell from
R2 0.40 with fixed exposure to R2 0.13 on auto.

What a phone can and cannot measure:

  can   -- canopy cover (green fraction), chromatic indices (GCC, ExG),
           plant counts, lesion morphology at macro range, growth-stage
           documentation, texture
  cannot -- reflectance, NIR, red edge, anything requiring absolute radiometry

For this system's bootstrap that is enough, because the growth staging carries
the rate calibration. Measured: with staging in place, forecast error at +21 d
was 11.6% with a 5-band multispectral and 11.7% with no camera at all. The
camera earns its place on DETECTION, not on the crop-growth forecast.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".dng", ".tif", ".tiff", ".heic"}


@dataclass
class Capture:
    """One photograph plus everything we could establish about how it was taken."""
    path: Path
    subplot: str
    kind: str                      # nadir | oblique | macro | context | greycard
    taken_at: datetime | None = None
    iso: float | None = None
    exposure_s: float | None = None
    f_number: float | None = None
    focal_mm: float | None = None
    white_balance: str | None = None
    lat: float | None = None
    lon: float | None = None
    width: int = 0
    height: int = 0
    sha1: str = ""

    @property
    def settings_key(self) -> tuple:
        """Identity of the capture settings, for the consistency check."""
        return (self.iso, self.exposure_s, self.f_number, self.white_balance)


def _exif(path: Path) -> dict:
    try:
        from PIL import Image, ExifTags
    except ImportError:
        return {}
    try:
        with Image.open(path) as im:
            w, h = im.size
            raw = im.getexif()
            out = {"width": w, "height": h}
            tags = {ExifTags.TAGS.get(k, k): v for k, v in raw.items()}
            ifd = raw.get_ifd(0x8769) if hasattr(raw, "get_ifd") else {}
            tags.update({ExifTags.TAGS.get(k, k): v for k, v in (ifd or {}).items()})
            gps = raw.get_ifd(0x8825) if hasattr(raw, "get_ifd") else {}
            out["tags"] = tags
            out["gps"] = {ExifTags.GPSTAGS.get(k, k): v for k, v in (gps or {}).items()}
            return out
    except Exception as e:
        log.debug("no EXIF from %s: %s", path.name, e)
        return {}


def _dms(v, ref) -> float | None:
    try:
        d, m, s = [float(x) for x in v]
        val = d + m / 60 + s / 3600
        return -val if str(ref).upper() in ("S", "W") else val
    except Exception:
        return None


def read_capture(path: Path, subplot: str, kind: str) -> Capture:
    meta = _exif(path)
    tags = meta.get("tags", {})
    gps = meta.get("gps", {})
    taken = None
    for key in ("DateTimeOriginal", "DateTime"):
        if key in tags:
            try:
                taken = datetime.strptime(str(tags[key]), "%Y:%m:%d %H:%M:%S")
                break
            except Exception:
                pass
    exp = tags.get("ExposureTime")
    try:
        exp = float(exp) if exp is not None else None
    except Exception:
        exp = None
    wb = tags.get("WhiteBalance")
    return Capture(
        path=path, subplot=subplot, kind=kind, taken_at=taken,
        iso=float(tags["ISOSpeedRatings"]) if "ISOSpeedRatings" in tags else None,
        exposure_s=exp,
        f_number=float(tags["FNumber"]) if "FNumber" in tags else None,
        focal_mm=float(tags["FocalLength"]) if "FocalLength" in tags else None,
        white_balance=("manual" if wb == 1 else "auto" if wb == 0 else None),
        lat=_dms(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef")),
        lon=_dms(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef")),
        width=meta.get("width", 0), height=meta.get("height", 0),
        sha1=hashlib.sha1(path.read_bytes()).hexdigest()[:12] if path.exists() else "",
    )


# ---------------------------------------------------------------------------
# Protocol compliance -- refuse to ingest a series shot on auto
# ---------------------------------------------------------------------------

@dataclass
class ProtocolReport:
    n_captures: int
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_protocol(captures: list[Capture], window=(11, 14),
                   max_settings_variants: int = 2) -> ProtocolReport:
    """Verify the capture discipline that the measurements depend on."""
    rep = ProtocolReport(n_captures=len(captures))
    if not captures:
        rep.problems.append("no captures found")
        return rep

    autos = [c for c in captures if c.white_balance == "auto"]
    if autos:
        rep.problems.append(
            f"{len(autos)}/{len(captures)} frames shot with AUTO white balance. "
            "Auto WB changes the colour of the scene as the canopy greens up, "
            "which is indistinguishable from the signal you are trying to "
            "measure. Reshoot with manual WB.")

    keys = {c.settings_key for c in captures if any(c.settings_key)}
    if len(keys) > max_settings_variants:
        rep.problems.append(
            f"{len(keys)} distinct exposure settings across the visit -- the "
            "camera was on auto-exposure. Fixed exposure is what makes frames "
            "comparable; on auto a published N retrieval fell from R2 0.40 to 0.13.")

    times = [c.taken_at for c in captures if c.taken_at]
    if times:
        outside = [t for t in times if not (window[0] <= t.hour < window[1])]
        if outside:
            rep.warnings.append(
                f"{len(outside)}/{len(times)} frames outside the {window[0]}:00-"
                f"{window[1]}:00 window; low sun changes the sunlit/shaded canopy "
                "mix enough to swamp index differences.")
        span_min = (max(times) - min(times)).total_seconds() / 60
        if span_min > 180:
            rep.warnings.append(
                f"visit spanned {span_min:.0f} min; sun moved appreciably between "
                "the first and last station.")
    else:
        rep.warnings.append("no EXIF timestamps -- cannot verify the time window.")

    if not any(c.kind == "greycard" for c in captures):
        rep.problems.append(
            "no grey-card frame. Without an in-scene reference there is no way to "
            "normalise between stations or between visits.")
    if not any(c.kind == "macro" for c in captures):
        rep.warnings.append("no macro frames -- lesion-scale disease detail missing.")
    return rep


# ---------------------------------------------------------------------------
# Measurements a phone can legitimately make
# ---------------------------------------------------------------------------

def load_rgb(path: Path, max_side: int = 1600) -> np.ndarray:
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        if max(im.size) > max_side:
            scale = max_side / max(im.size)
            im = im.resize((int(im.width * scale), int(im.height * scale)))
        return np.asarray(im, dtype=np.float32) / 255.0


def grey_card_gains(path: Path, target: float = 0.18,
                    centre_frac: float = 0.35) -> dict[str, float]:
    """Per-channel gains from a grey card filling the centre of the frame.

    This is the phone's substitute for a calibrated panel. It does not give
    absolute reflectance -- the phone's tone curve is nonlinear and unknown --
    but it does remove the between-station colour cast, which is what makes
    ExG and GCC comparable across the visit.
    """
    img = load_rgb(path)
    h, w, _ = img.shape
    dh, dw = int(h * centre_frac / 2), int(w * centre_frac / 2)
    patch = img[h // 2 - dh: h // 2 + dh, w // 2 - dw: w // 2 + dw]
    means = patch.reshape(-1, 3).mean(axis=0)
    if np.any(means < 1e-4):
        raise ValueError(f"grey card frame {path.name} is underexposed")
    if np.any(means > 0.97):
        raise ValueError(f"grey card frame {path.name} is clipped -- reduce exposure")
    return {c: float(target / m) for c, m in zip("rgb", means)}


def apply_gains(img: np.ndarray, gains: dict[str, float]) -> np.ndarray:
    out = img.copy()
    for i, c in enumerate("rgb"):
        out[..., i] *= gains.get(c, 1.0)
    return np.clip(out, 0.0, 1.0)


@dataclass
class SubplotMeasurement:
    subplot: str
    canopy_cover: float
    gcc: float
    exg_mean: float
    n_frames: int
    grey_card_used: bool
    lai_proxy: float | None = None
    lai_sd: float | None = None
    note: str = ""


def measure_subplot(subplot: str, nadir_paths: list[Path],
                    gains: dict[str, float] | None = None) -> SubplotMeasurement:
    from .retrieval import DEFAULT_MODELS, canopy_cover, exg, gcc

    covers, gccs, exgs = [], [], []
    for p in nadir_paths:
        img = load_rgb(p)
        if gains:
            img = apply_gains(img, gains)
        r, g, b = img[..., 0], img[..., 1], img[..., 2]
        covers.append(canopy_cover(r, g, b))
        gccs.append(float(np.median(gcc(r, g, b))))
        exgs.append(float(np.mean(exg(r, g, b))))
    if not covers:
        raise ValueError(f"no nadir frames for subplot {subplot}")

    m = SubplotMeasurement(subplot=subplot, canopy_cover=float(np.mean(covers)),
                           gcc=float(np.mean(gccs)), exg_mean=float(np.mean(exgs)),
                           n_frames=len(covers), grey_card_used=bool(gains))
    model = DEFAULT_MODELS["gcc"]
    m.lai_proxy = model.predict(m.gcc)
    m.lai_sd = model.observation_sd(m.lai_proxy)
    if m.lai_proxy >= (model.saturates_above_lai or 99):
        m.note = ("GCC has saturated -- treat this LAI as a lower bound. Canopy "
                  "cover is the more trustworthy number at this stage.")
    if not gains:
        m.note += (" No grey card applied; indices are comparable within this "
                   "frame set only.")
    return m
