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


# --- tank capacity and spot spraying (audit S5: repo planned 6 ha broadcast as 1 sortie) ---

from fa_planner.routing import spray_drone  # noqa: E402

SIX_HA = Polygon([(0, 0), (300, 0), (300, 200), (0, 200)])


def _spray_swaths(field=SIX_HA):
    return order_boustrophedon(plan_coverage(field, 4.0).swaths)


def test_tank_forces_refill_sorties_for_broadcast():
    sw = _spray_swaths()
    d = spray_drone(tank_l=10.0, rate_l_ha=20.0)
    plan = plan_sorties(sw, (0.0, 0.0), d, time_limit_s=10)
    total = sum(s.liquid_l for s in plan.sorties)
    assert total == pytest.approx(6.0 * 20.0, rel=0.05)        # ~120 L on 6 ha
    assert plan.n_sorties >= math.ceil(total / 10.0)            # >= 12 tank loads
    assert verify(plan) == []
    assert all(s.liquid_l <= 10.0 + 1e-6 for s in plan.sorties)


def test_bigger_tank_needs_fewer_sorties():
    sw = _spray_swaths()
    small = plan_sorties(sw, (0.0, 0.0), spray_drone(tank_l=10.0), time_limit_s=10)
    big = plan_sorties(sw, (0.0, 0.0), spray_drone(tank_l=16.0), time_limit_s=10)
    assert big.n_sorties < small.n_sorties


def test_spot_spray_skips_clean_swaths_and_uses_less_liquid():
    sw = _spray_swaths()
    frac = {s.index: (0.3 if s.index % 4 == 0 else 0.0) for s in sw}
    plan = plan_sorties(sw, (0.0, 0.0), spray_drone(tank_l=10.0), treat_fraction=frac,
                        time_limit_s=10)
    flown = {x.index for s in plan.sorties for x in s.swaths}
    assert flown == {i for i, f in frac.items() if f > 0}
    assert plan.summary()["mode"] == "spot"
    assert sum(s.liquid_l for s in plan.sorties) < 0.1 * 120
    assert verify(plan) == []


def test_verifier_catches_an_overfilled_sortie():
    sw = _spray_swaths()
    d = spray_drone(tank_l=10.0)
    plan = plan_sorties(sw, (0.0, 0.0), d, time_limit_s=5)
    merged = plan.sorties[0]
    merged.swaths = merged.swaths + plan.sorties[1].swaths
    assert any("tank" in p for p in verify(plan))


def test_scouting_never_flies_when_there_is_no_spray_decision_to_change():
    """Review minor: with no sprayable day / active protectant, a flight cannot change anything."""
    import datetime as dt
    from fa_planner.weekly import FieldStatus, scouting_trigger
    from fa_twin.weather import generate_records
    plant = dt.date(2025, 10, 25)
    members = [generate_records(dt.date(2025, 10, 1), 200, s) for s in range(10)]
    st = FieldStatus(day=dt.date(2025, 12, 20), plant=plant, harvest=dt.date(2026, 2, 10))
    fly, why = scouting_trigger(members, st, threshold=0.0, p_spray_better=None)
    assert not fly and "no spray decision" in why


# --- review #4: the planner must use the blight information it holds ---

def _blight_weather(fav_from, fav_to, seed=3):
    """Dry winter weather with a forced blight-favourable spell."""
    import datetime as dt
    from fa_twin.weather import generate_records
    recs = generate_records(dt.date(2025, 10, 1), 200, seed)
    out = []
    for r in recs:
        if fav_from <= r["DAY"] <= fav_to:
            r = dict(r, TMAX=20.0, TMIN=10.0, RAIN=5.0, VAP=12.0)
        else:
            r = dict(r, RAIN=0.0, VAP=min(r["VAP"], 6.0), TMIN=max(r["TMIN"], 7.0))
        out.append(r)
    return out


def test_scouted_severity_is_carried_forward_not_restarted():
    import datetime as dt
    import numpy as np
    from fa_planner.weekly import FieldStatus, member_blight_state
    plant = dt.date(2025, 10, 25)
    recs = _blight_weather(dt.date(2025, 12, 1), dt.date(2025, 12, 20))
    st = FieldStatus(day=dt.date(2025, 12, 15), plant=plant, harvest=dt.date(2026, 2, 10),
                     scouting=[(dt.date(2025, 12, 3), 0.02)])
    S = [member_blight_state(recs, st, np.random.default_rng(k)).S for k in range(20)]
    assert np.median(S) > 0.1          # 12 favourable days at r=0.3 grow 0.02 well past 0.1


def test_unseen_past_infection_is_inferred_from_observed_weather():
    import datetime as dt
    import numpy as np
    from fa_planner.weekly import FieldStatus, member_blight_state
    plant = dt.date(2025, 10, 25)
    recs = _blight_weather(dt.date(2025, 12, 1), dt.date(2025, 12, 12))
    st = FieldStatus(day=dt.date(2025, 12, 15), plant=plant, harvest=dt.date(2026, 2, 10),
                     inoculum=True)
    b = member_blight_state(recs, st, np.random.default_rng(0))
    assert b.inf_day is not None and b.inf_day < st.day and b.S > 0.001


def test_negative_scouting_rules_out_visible_infections():
    import datetime as dt
    import numpy as np
    from fa_planner.weekly import DETECTION_LIMIT, FieldStatus, member_blight_state
    plant = dt.date(2025, 10, 25)
    recs = _blight_weather(dt.date(2025, 12, 1), dt.date(2025, 12, 25))
    neg = dt.date(2025, 12, 14)
    st = FieldStatus(day=dt.date(2025, 12, 15), plant=plant, harvest=dt.date(2026, 2, 10),
                     inoculum=True, scouting=[(neg, None)])
    for k in range(10):
        b = member_blight_state(recs, st, np.random.default_rng(k), trace=True)
        assert b.trace.get(neg, 0.0) < DETECTION_LIMIT
