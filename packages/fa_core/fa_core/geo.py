"""Geometry, projections and imaging geometry.

The imaging-geometry functions here are the ones that decide whether a mission
is worth flying at all: ground sample distance, footprint, line spacing for a
required overlap, and the resulting frame count. Getting these wrong is how you
discover after the flight that your 3 cm target GSD was actually 7 cm, or that
your photogrammetry has nothing to match on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from pyproj import CRS, Transformer
from shapely.geometry import Polygon, MultiPolygon, shape
from shapely.ops import transform as shp_transform

WGS84 = CRS.from_epsg(4326)


def utm_crs_for(lon: float, lat: float) -> CRS:
    """Local UTM zone, so areas and distances are in metres and not degrees."""
    zone = int(math.floor((lon + 180) / 6) % 60) + 1
    epsg = (32600 if lat >= 0 else 32700) + zone
    return CRS.from_epsg(epsg)


def to_local(geom, crs: CRS | None = None):
    """Project a lon/lat geometry to its local UTM zone."""
    if isinstance(geom, dict):
        geom = shape(geom)
    c = geom.centroid
    crs = crs or utm_crs_for(c.x, c.y)
    tf = Transformer.from_crs(WGS84, crs, always_xy=True).transform
    return shp_transform(tf, geom), crs


def to_wgs84(geom, crs: CRS):
    tf = Transformer.from_crs(crs, WGS84, always_xy=True).transform
    return shp_transform(tf, geom)


def area_ha(geom) -> float:
    """Area in hectares, computed in a local projection (never in degrees)."""
    g, _ = to_local(geom)
    return g.area / 10_000.0


@dataclass(frozen=True)
class Camera:
    """A survey camera, described by the numbers that actually matter."""
    name: str
    sensor_width_mm: float
    sensor_height_mm: float
    focal_length_mm: float
    image_width_px: int
    image_height_px: int
    bands: tuple[str, ...] = ("R", "G", "B")

    @property
    def pixel_pitch_mm(self) -> float:
        return self.sensor_width_mm / self.image_width_px

    def gsd_m(self, altitude_agl_m: float) -> float:
        """Ground sample distance in metres/pixel at nadir."""
        return (self.sensor_width_mm * altitude_agl_m) / (
            self.focal_length_mm * self.image_width_px
        )

    def altitude_for_gsd(self, gsd_m: float) -> float:
        return (gsd_m * self.focal_length_mm * self.image_width_px) / self.sensor_width_mm

    def footprint_m(self, altitude_agl_m: float) -> tuple[float, float]:
        g = self.gsd_m(altitude_agl_m)
        return g * self.image_width_px, g * self.image_height_px

    def hfov_deg(self) -> float:
        return math.degrees(2 * math.atan(self.sensor_width_mm / (2 * self.focal_length_mm)))


# Real cameras. Values from published sensor specifications; verify against the
# unit you actually fly, because "1/2.3 inch" is not a number.
CAMERAS = {
    # DJI Mavic 3M multispectral: 4 narrowband sensors + RGB.
    "mavic3m_ms": Camera("DJI Mavic 3M (multispectral)", 6.4, 4.8, 4.34, 2592, 1944,
                         ("G", "R", "RE", "NIR")),
    "mavic3m_rgb": Camera("DJI Mavic 3M (RGB)", 17.3, 13.0, 12.29, 5280, 3956),
    # MicaSense RedEdge-P.
    "rededge_p": Camera("MicaSense RedEdge-P", 7.12, 5.33, 5.4, 1456, 1088,
                        ("B", "G", "R", "RE", "NIR")),
    # Generic 20 MP RGB survey camera (Phantom 4 RTK class).
    "p4rtk": Camera("Phantom 4 RTK class", 13.2, 8.8, 8.8, 5472, 3648),
    # The PX4 SITL default, so simulation numbers are honest about what they are.
    "sitl_mono": Camera("PX4 gz mono_cam (stock)", 3.68, 2.76, 1.88, 1280, 960),
    # The bootstrap sensor: a current mid-range phone main camera, 1/1.7" class,
    # 24 mm-equivalent, writing binned 12 MP frames. Handheld on a monopod this
    # is the whole sensing hardware for the first campaign. Measure YOUR phone --
    # EXIF FocalLength and FocalLengthIn35mmFilm give the crop factor, and the
    # sensor width follows; do not trust a spec sheet's "1/1.7 inch".
    "phone_main": Camera("Phone main camera (1/1.7\" class)", 7.6, 5.7, 5.4, 4000, 3000),
}


@dataclass(frozen=True)
class SurveyPlan:
    altitude_m: float
    gsd_m: float
    line_spacing_m: float
    trigger_distance_m: float
    swath_m: float
    front_overlap: float
    side_overlap: float

    def frames_for_area(self, area_m2: float) -> int:
        per_frame = self.line_spacing_m * self.trigger_distance_m
        return max(1, math.ceil(area_m2 / per_frame))


def plan_survey(
    camera: Camera,
    target_gsd_m: float,
    front_overlap: float = 0.75,
    side_overlap: float = 0.65,
) -> SurveyPlan:
    """Turn a target GSD and overlap into altitude, line spacing and trigger distance.

    Overlap defaults are deliberately high (75/65) because that is what
    photogrammetry over homogeneous crop canopy needs -- canopy is the hardest
    case for feature matching, and low overlap is where reconstructions fail.
    The published tradeoff is stark: dropping to 40/30 cut processing time ~10x
    at identical GSD in one controlled study, so treat overlap as a tunable and
    sweep it, but do not start low.
    """
    if not (0 <= front_overlap < 1 and 0 <= side_overlap < 1):
        raise ValueError("overlaps must be fractions in [0, 1)")
    alt = camera.altitude_for_gsd(target_gsd_m)
    fw, fh = camera.footprint_m(alt)
    return SurveyPlan(
        altitude_m=alt,
        gsd_m=target_gsd_m,
        line_spacing_m=fw * (1 - side_overlap),
        trigger_distance_m=fh * (1 - front_overlap),
        swath_m=fw,
        front_overlap=front_overlap,
        side_overlap=side_overlap,
    )


def oblique_gsd_m(camera: Camera, height_m: float, ground_range_m: float) -> float:
    """GSD of a fixed mast/pole camera looking out at a target on the ground.

    This is the function that kills the "just put a camera on a mast instead of
    flying" idea, and it is worth being able to show rather than assert. A nadir
    view has slant range = height. An oblique view at range R has slant range
    sqrt(h^2 + R^2) and the ground is foreshortened by the depression angle, so
    resolution degrades roughly as R^2/h for R >> h.
    """
    if height_m <= 0:
        raise ValueError("height must be positive")
    slant = math.hypot(height_m, ground_range_m)
    depression = math.atan2(height_m, ground_range_m)  # radians above horizontal
    ifov = camera.pixel_pitch_mm / camera.focal_length_mm   # radians per pixel
    across = slant * ifov
    along = slant * ifov / max(math.sin(depression), 1e-6)
    return math.sqrt(across * along)


def mast_range_for_gsd(camera: Camera, height_m: float, target_gsd_m: float,
                       max_range_m: float = 5000.0) -> float:
    """Furthest ground range at which a mast camera still meets ``target_gsd_m``."""
    lo, hi = 0.1, max_range_m
    if oblique_gsd_m(camera, height_m, lo) > target_gsd_m:
        return 0.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if oblique_gsd_m(camera, height_m, mid) <= target_gsd_m:
            lo = mid
        else:
            hi = mid
    return lo


def coverable_area_ha(camera: Camera, height_m: float, target_gsd_m: float) -> float:
    r = mast_range_for_gsd(camera, height_m, target_gsd_m)
    return math.pi * r * r / 10_000.0
