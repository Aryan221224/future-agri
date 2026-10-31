"""Imaging geometry: the numbers that decide whether a mission is worth flying."""
import math
import pytest
from shapely.geometry import Polygon

from fa_core.geo import (CAMERAS, area_ha, coverable_area_ha, mast_range_for_gsd,
                         oblique_gsd_m, plan_survey, to_local, utm_crs_for)


def test_gsd_and_altitude_are_inverses():
    cam = CAMERAS["mavic3m_ms"]
    for alt in (20, 60, 120):
        assert math.isclose(cam.altitude_for_gsd(cam.gsd_m(alt)), alt, rel_tol=1e-9)


def test_gsd_scales_linearly_with_altitude():
    cam = CAMERAS["p4rtk"]
    assert math.isclose(cam.gsd_m(120), 2 * cam.gsd_m(60), rel_tol=1e-9)


def test_area_uses_a_projected_crs_not_degrees():
    p = Polygon([(75.85, 30.90), (75.86, 30.90), (75.86, 30.91), (75.85, 30.91)])
    ha = area_ha(p)
    assert 80 < ha < 130          # ~1.11 km x 0.96 km
    _, crs = to_local(p)
    assert crs.to_epsg() == 32643  # UTM 43N


def test_utm_zone_selection_north_and_south():
    assert utm_crs_for(75.85, 30.9).to_epsg() == 32643
    assert utm_crs_for(75.85, -30.9).to_epsg() == 32743


def test_overlap_increases_frame_count():
    cam = CAMERAS["mavic3m_rgb"]
    low = plan_survey(cam, 0.03, front_overlap=0.40, side_overlap=0.30)
    high = plan_survey(cam, 0.03, front_overlap=0.80, side_overlap=0.70)
    assert high.frames_for_area(1e5) > 4 * low.frames_for_area(1e5)
    assert math.isclose(low.altitude_m, high.altitude_m)   # GSD fixes altitude


def test_invalid_overlap_rejected():
    with pytest.raises(ValueError):
        plan_survey(CAMERAS["p4rtk"], 0.03, front_overlap=1.0)


def test_mast_cannot_substitute_for_aerial():
    """The quantitative version of 'why not just use a pole camera'."""
    cam = CAMERAS["mavic3m_ms"]
    for height in (5, 10, 20):
        assert coverable_area_ha(cam, height, 0.03) < 1.0
    # Resolution degrades with range at fixed mast height.
    assert oblique_gsd_m(cam, 10, 100) > oblique_gsd_m(cam, 10, 20)
    assert mast_range_for_gsd(cam, 20, 0.03) > mast_range_for_gsd(cam, 5, 0.03)
