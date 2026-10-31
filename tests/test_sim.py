"""Kinematic flight: the energy model must agree with what the router planned."""
from shapely.geometry import Polygon

from fa_core.geo import CAMERAS, plan_survey
from fa_planner.coverage import order_boustrophedon, plan_coverage
from fa_planner.routing import DroneModel, plan_sorties
from fa_sim.kinematic import KinematicDrone

FIELD = Polygon([(0, 0), (400, 0), (400, 250), (0, 250)])
SP = plan_survey(CAMERAS["mavic3m_ms"], 0.03)


def _mission(drone):
    p = plan_coverage(FIELD, SP.line_spacing_m)
    return plan_sorties(order_boustrophedon(p.swaths), (0.0, 0.0), drone, time_limit_s=5)


def test_flight_energy_matches_the_plan_within_tolerance():
    d = DroneModel()
    mission = _mission(d)
    sim = KinematicDrone(d, home=(0.0, 0.0), altitude_m=SP.altitude_m,
                         trigger_distance_m=SP.trigger_distance_m)
    for planned, flown in zip(mission.sorties, sim.fly_mission(mission)):
        assert flown.completed
        assert abs(flown.energy_wh - planned.energy_wh) < 0.1 * planned.energy_wh


def test_frames_are_captured_at_the_planned_interval():
    d = DroneModel()
    mission = _mission(d)
    sim = KinematicDrone(d, home=(0.0, 0.0), altitude_m=SP.altitude_m,
                         trigger_distance_m=SP.trigger_distance_m)
    flights = sim.fly_mission(mission)
    total_swath_m = sum(sw.length_m for s in mission.sorties for sw in s.swaths)
    expected = total_swath_m / SP.trigger_distance_m
    got = sum(f.n_frames for f in flights)
    assert 0.7 * expected <= got <= 1.3 * expected


def test_flight_aborts_rather_than_exceeding_reserve():
    """A drone that would run flat must report failure, not fly anyway."""
    planned = _mission(DroneModel())
    starved = DroneModel(usable_energy_wh=40.0)
    sim = KinematicDrone(starved, home=(0.0, 0.0))
    results = sim.fly_mission(planned)
    assert any(not r.completed for r in results)
    assert all(r.aborted_reason for r in results if not r.completed)
