"""Field identity must survive how people actually draw boundaries."""
import pytest
from shapely.geometry import Polygon

from fa_core.geoid import compare, geoid

BASE = Polygon([(75.850, 30.900), (75.855, 30.900), (75.855, 30.905), (75.850, 30.905)])


def test_stable_under_winding_and_start_vertex():
    reversed_ring = Polygon(list(BASE.exterior.coords)[::-1])
    rotated = Polygon(list(BASE.exterior.coords)[2:-1] + list(BASE.exterior.coords)[:2])
    assert geoid(BASE) == geoid(reversed_ring) == geoid(rotated)


def test_stable_under_sub_metre_jitter():
    jittered = Polygon([(x + 1e-6, y - 1e-6) for x, y in BASE.exterior.coords])
    assert geoid(BASE) == geoid(jittered)


def test_different_fields_differ():
    other = Polygon([(75.86, 30.91), (75.865, 30.91), (75.865, 30.915), (75.86, 30.915)])
    assert geoid(BASE) != geoid(other)
    assert compare(BASE, other).jaccard == 0.0
    assert not compare(BASE, other).same_field


def test_overlap_is_graded():
    shifted = Polygon([(x + 0.0005, y) for x, y in BASE.exterior.coords])
    m = compare(BASE, shifted)
    assert 0.0 < m.jaccard < 1.0


def test_empty_geometry_rejected():
    with pytest.raises(ValueError):
        geoid(Polygon())
