#!/usr/bin/env python3
"""What can a mast-mounted camera at a field corner actually see?

The system's sensing front end is FIXED: cameras on masts at the field corners,
scanning the field continuously and feeding the twin. That is a fundamentally
different geometry from a nadir aerial survey, and it has two hard limits that
decide the whole hardware specification.

1. RESOLUTION falls off with range. Ground sample distance at range R from a
   camera of focal length f grows roughly as R^2/(h*f) once R >> h, because
   both the slant range and the foreshortening get worse together.

2. VIEWING GEOMETRY fails before resolution does. At a shallow depression
   angle the camera looks *through* the canopy rather than *into* it: near
   plants occlude far ones, you see the side of the canopy instead of the top,
   and the relationship between what you measure and LAI stops being the one
   your retrieval was calibrated on. This constraint binds first, and it is the
   one most fixed-camera proposals ignore.

This script quantifies both so the mast height, lens and count fall out of the
requirement instead of being guessed.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "fa_core"))

from fa_core.geo import Camera  # noqa: E402

# Minimum depression angle for a measurement that still behaves like a
# top-of-canopy observation. Below ~20 deg the view is dominated by canopy
# sides and inter-plant occlusion; below ~15 deg it is effectively a horizontal
# view through the crop.
MIN_DEPRESSION_DEG = 20.0


@dataclass(frozen=True)
class MastCamera:
    name: str
    sensor_width_mm: float
    sensor_height_mm: float
    focal_mm: float
    px_w: int
    px_h: int
    bands: str
    note: str = ""

    @property
    def ifov_rad(self) -> float:
        return (self.sensor_width_mm / self.px_w) / self.focal_mm

    @property
    def hfov_deg(self) -> float:
        return math.degrees(2 * math.atan(self.sensor_width_mm / (2 * self.focal_mm)))

    def gsd_at(self, height_m: float, range_m: float) -> float:
        """Geometric-mean GSD of an oblique view at ground range R."""
        slant = math.hypot(height_m, range_m)
        depression = math.atan2(height_m, max(range_m, 1e-6))
        across = slant * self.ifov_rad
        along = across / max(math.sin(depression), 1e-6)
        return math.sqrt(across * along)

    def max_range_for_gsd(self, height_m: float, target_gsd_m: float,
                          cap_m: float = 3000.0) -> float:
        lo, hi = 0.5, cap_m
        if self.gsd_at(height_m, lo) > target_gsd_m:
            return 0.0
        for _ in range(120):
            mid = (lo + hi) / 2
            if self.gsd_at(height_m, mid) <= target_gsd_m:
                lo = mid
            else:
                hi = mid
        return lo


def geometry_range(height_m: float, min_depression_deg: float = MIN_DEPRESSION_DEG) -> float:
    """Furthest ground range still viewed at an acceptable depression angle."""
    return height_m / math.tan(math.radians(min_depression_deg))


def corner_coverage_m2(radius_m: float, field_w: float, field_h: float,
                       n_corners: int = 4, samples: int = 600) -> float:
    """Area of a rectangular field covered by quarter-disc sectors at its corners.

    Monte-Carlo-free grid integration, because the sectors overlap for large
    radii and the closed form is fiddly.
    """
    corners = [(0, 0), (field_w, 0), (0, field_h), (field_w, field_h)][:n_corners]
    dx, dy = field_w / samples, field_h / samples
    hit = 0
    for i in range(samples):
        x = (i + 0.5) * dx
        for j in range(samples):
            y = (j + 0.5) * dy
            if any(math.dist((x, y), c) <= radius_m for c in corners):
                hit += 1
                break_flag = True
    return hit * dx * dy


# Candidate mast cameras. A fixed installation has no weight or power budget to
# respect, so the lens can be far longer than anything you would fly -- which is
# the single biggest lever available on the resolution constraint.
CANDIDATES = [
    MastCamera("RedEdge-P (stock 5.4 mm)", 7.12, 5.33, 5.4, 1456, 1088, "5-band+pan",
               "the aerial sensor, unmodified"),
    MastCamera("RedEdge-P + 25 mm optic", 7.12, 5.33, 25.0, 1456, 1088, "5-band+pan",
               "same sensor, telephoto"),
    MastCamera("Machine-vision 5 MP + 50 mm", 8.45, 7.07, 50.0, 2448, 2048, "RGB or filtered",
               "e.g. Sony IMX264 global shutter"),
    MastCamera("Machine-vision 5 MP + 75 mm", 8.45, 7.07, 75.0, 2448, 2048, "RGB or filtered",
               "narrower FOV, more pan steps"),
    MastCamera("20 MP RGB + 35 mm", 13.2, 8.8, 35.0, 5472, 3648, "RGB",
               "for weed/texture work"),
]

FIELD_W, FIELD_H = 700.0, 420.0     # the 29.3 ha demo field
FIELD_A = FIELD_W * FIELD_H

print(f"Field: {FIELD_W:.0f} x {FIELD_H:.0f} m = {FIELD_A/1e4:.1f} ha")
print(f"Diagonal half-length (corner to centre): {math.hypot(FIELD_W, FIELD_H)/2:.0f} m")
print(f"Acceptable-geometry limit: depression >= {MIN_DEPRESSION_DEG:.0f} deg\n")

print("=" * 92)
print("CONSTRAINT 1 -- viewing geometry (independent of lens)")
print("=" * 92)
print(f"{'mast height':>12s} {'usable radius':>14s} {'4-corner coverage of 29.4 ha':>32s}")
for h in (4, 6, 10, 15, 20, 30):
    r = geometry_range(h)
    cov = corner_coverage_m2(r, FIELD_W, FIELD_H)
    print(f"{h:9d} m {r:12.0f} m {cov/1e4:20.2f} ha  ({cov/FIELD_A:5.1%})")

print("\n" + "=" * 92)
print("CONSTRAINT 2 -- resolution, at the range geometry already allows")
print("=" * 92)
for h in (6, 10, 15):
    r_geom = geometry_range(h)
    print(f"\n  mast {h} m  (geometry allows out to {r_geom:.0f} m)")
    print(f"    {'camera':<30s} {'HFOV':>6s} {'GSD@edge':>10s} {'r@5cm':>8s} {'r@2cm':>8s} {'binds':>10s}")
    for c in CANDIDATES:
        g_edge = c.gsd_at(h, r_geom)
        r5 = c.max_range_for_gsd(h, 0.05)
        r2 = c.max_range_for_gsd(h, 0.02)
        binds = "geometry" if r5 >= r_geom else "resolution"
        print(f"    {c.name:<30s} {c.hfov_deg:5.0f}d {g_edge*100:8.1f} cm "
              f"{r5:6.0f} m {r2:6.0f} m {binds:>10s}")

print("\n" + "=" * 92)
print("WHAT IT WOULD TAKE TO COVER THE WHOLE FIELD FROM MASTS")
print("=" * 92)
for h in (6, 10, 15):
    r = geometry_range(h)
    # Hexagonal packing of discs of radius r covers area ~ 2.6 r^2 per mast.
    per_mast = 2.598 * r * r
    n = math.ceil(FIELD_A / per_mast)
    print(f"  {h:2d} m masts, usable radius {r:5.1f} m -> ~{n:4d} masts for full coverage "
          f"(spacing ~{r*1.73:.0f} m)")

print("\n" + "=" * 92)
print("THE SAME FIELD, SCANNED FROM THE AIR (for comparison)")
print("=" * 92)
from fa_core.geo import CAMERAS, plan_survey
for gsd_cm, label in ((5.0, "LAI at zone scale"), (2.0, "individual weed detection")):
    sp = plan_survey(CAMERAS["mavic3m_ms"], gsd_cm / 100)
    print(f"  {label:28s} -> {sp.altitude_m:5.0f} m altitude, "
          f"{sp.frames_for_area(FIELD_A):5d} frames, 100% of the field")
