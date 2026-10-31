"""Coverage and energy-constrained routing."""
import math
import pytest
from shapely.geometry import Polygon

from fa_core.geo import CAMERAS, plan_survey
from fa_planner.coverage import (best_angle, order_boustrophedon, path_length_m,
                                 plan_coverage)
from fa_planner.routing import DroneModel, plan_sorties, verify

FIELD = Polygon([(0, 0), (400, 0), (400, 250), (0, 250)])
SP = plan_survey(CAMERAS["mavic3m_ms"], 0.03)


def test_swaths_cover_the_working_area():
    p = plan_coverage(FIELD, SP.line_spacing_m)
    assert p.coverage_ratio > 0.9
    assert len(p.swaths) >= math.floor(250 / SP.line_spacing_m) - 1


def test_best_angle_minimises_swath_count():
    p_best = plan_coverage(FIELD, SP.line_spacing_m)
    p_bad = plan_coverage(FIELD, SP.line_spacing_m, angle_deg=90.0)
    assert len(p_best.swaths) <= len(p_bad.swaths)


def test_handles_non_convex_field_with_obstacle():
    holed = FIELD.difference(Polygon([(150, 100), (220, 100), (220, 160), (150, 160)]))
    p = plan_coverage(holed, SP.line_spacing_m)
    assert len(p.swaths) > 0
    # Swaths must not cross the obstacle.
    for s in p.swaths:
        assert s.line.length > 0
        assert not s.line.crosses(Polygon([(155, 105), (215, 105), (215, 155), (155, 155)]))


def test_boustrophedon_alternates_direction():
    p = plan_coverage(FIELD, SP.line_spacing_m)
    o = order_boustrophedon(p.swaths)
    # Consecutive swaths should end near where the next begins.
    gaps = [math.dist(a.end, b.start) for a, b in zip(o, o[1:])]
    naive = [math.dist(a.end, b.start) for a, b in zip(p.swaths, p.swaths[1:])]
    assert sum(gaps) < sum(naive)


def test_headland_defaults_to_zero_for_multirotor():
    p = plan_coverage(FIELD, SP.line_spacing_m)
    assert p.headland_area_m2 == 0.0
    p1 = plan_coverage(FIELD, SP.line_spacing_m, headland_passes=1)
    assert p1.headland_area_m2 > 0.0


def test_every_sortie_respects_the_energy_budget():
    p = plan_coverage(FIELD, SP.line_spacing_m)
    d = DroneModel()
    plan = plan_sorties(order_boustrophedon(p.swaths), (0.0, 0.0), d, time_limit_s=5)
    assert plan.n_sorties >= 1
    assert verify(plan) == []
    for s in plan.sorties:
        assert s.energy_wh <= d.usable_energy_wh


def test_every_swath_is_flown_exactly_once():
    p = plan_coverage(FIELD, SP.line_spacing_m)
    plan = plan_sorties(order_boustrophedon(p.swaths), (0.0, 0.0), DroneModel(),
                        time_limit_s=5)
    flown = [sw.index for s in plan.sorties for sw in s.swaths]
    assert sorted(flown) == sorted(sw.index for sw in p.swaths)


def test_smaller_battery_forces_more_sorties():
    p = plan_coverage(FIELD, SP.line_spacing_m)
    o = order_boustrophedon(p.swaths)
    big = plan_sorties(o, (0.0, 0.0), DroneModel(usable_energy_wh=400), time_limit_s=5)
    small = plan_sorties(o, (0.0, 0.0), DroneModel(usable_energy_wh=120), time_limit_s=5)
    assert small.n_sorties > big.n_sorties
    assert verify(small) == [] and verify(big) == []


def test_infeasible_mission_raises_rather_than_returning_a_bad_plan():
    p = plan_coverage(FIELD, SP.line_spacing_m)
    tiny = DroneModel(usable_energy_wh=8.0)
    with pytest.raises(RuntimeError):
        plan_sorties(order_boustrophedon(p.swaths), (0.0, 0.0), tiny,
                     max_sorties=2, time_limit_s=3)
