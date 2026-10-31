"""Energy-constrained multi-sortie routing over coverage swaths.

The gap this fills
------------------
Fields2Cover plans a route over swaths for one vehicle with no endurance limit.
Its ``Robot`` type has width, speed and turning radius and no battery, capacity
or refill concept anywhere in the API. But an agricultural multirotor flies
15-40 minutes and a real field needs hours, so *every* mission is multi-sortie
and the endurance constraint is the binding one. That decomposition is ours.

How it is modelled
------------------
OR-Tools has no native "vehicle returns to depot, swaps battery and continues"
concept. The established workaround, and the one used here, is to replicate the
drone as N **virtual vehicles**, one per sortie, each starting and ending at the
depot with a full battery (``start_cumul_to_zero=True`` on the energy
dimension). Battery swap time is charged as a fixed cost per sortie used, which
also makes the solver prefer fewer sorties.

Two details that are easy to get wrong and expensive to get wrong:

* **Energy must be an arc dimension, not a node demand.** Battery drain depends
  on the transition -- distance flown, whether the sprayer is running -- not on
  visiting a node. Using the unary demand callback from the CVRP tutorial gives
  a model that cannot represent flying further to reach a swath.
* **Spraying adds a second capacity: the tank.** A 10 L tank at 20 L/ha covers
  0.5 ha, so a 6 ha broadcast job needs ~14 sorties however big the battery is
  (the 30 Oct audit caught the repo planning that job as 1 sortie, because only
  energy was modelled). Liquid is a node demand (it is consumed by spraying a
  swath, not by flying between swaths), capped per sortie by the tank. In
  **spot-spray** mode each swath carries the fraction of it that needs
  treatment (from the weed map); untreated swaths are not flown at all.
* **Swath direction matters.** A swath can be entered from either end, and the
  two choices have different transition costs. Modelling each swath as a single
  node throws that away. We model each swath as a pair of nodes (the two
  entries) and force exactly one to be visited via a disjunction, which is what
  keeps the serpentine structure the coverage planner intended.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .coverage import Swath

log = logging.getLogger(__name__)


@dataclass
class DroneModel:
    """Energy model of the aircraft.

    Defaults are a small agricultural multirotor. **Calibrate these against
    real flight logs before trusting any route.** A 20% energy-model error
    strands an aircraft in a field, which is a more likely and more expensive
    failure than anything the routing algorithm will prevent -- published
    energy-aware UAV coverage work gets to ~97% accuracy with an empirical
    model fitted to real flights, and that is the bar.
    """
    cruise_speed_ms: float = 8.0
    survey_speed_ms: float = 6.0
    usable_energy_wh: float = 220.0        # after reserve
    cruise_power_w: float = 620.0
    survey_power_w: float = 700.0          # payload + lower speed
    turn_energy_wh: float = 0.35           # per turn, accel/decel
    takeoff_landing_wh: float = 6.0
    battery_swap_s: float = 180.0
    reserve_fraction: float = 0.20         # of nominal pack, already excluded above
    # Spraying. ``tank_l=None`` means a survey aircraft (no liquid constraint).
    tank_l: float | None = None
    application_rate_l_ha: float = 20.0
    spray_swath_m: float = 4.0

    def swath_liquid_l(self, length_m: float, fraction: float = 1.0) -> float:
        return length_m * self.spray_swath_m * self.application_rate_l_ha / 10_000.0 * fraction

    def transit_wh(self, distance_m: float) -> float:
        return (distance_m / self.cruise_speed_ms) * self.cruise_power_w / 3600.0

    def survey_wh(self, distance_m: float) -> float:
        return (distance_m / self.survey_speed_ms) * self.survey_power_w / 3600.0

    def transit_s(self, distance_m: float) -> float:
        return distance_m / self.cruise_speed_ms

    def survey_s(self, distance_m: float) -> float:
        return distance_m / self.survey_speed_ms


def spray_drone(tank_l: float = 10.0, rate_l_ha: float = 20.0, swath_m: float = 4.0) -> DroneModel:
    """A 10-16 L agricultural spray multirotor (audit settings: 4 m swath, 5 m/s,
    ~12 min endurance with payload). **Calibrate against the service provider's
    aircraft and logs before trusting a route.**"""
    return DroneModel(cruise_speed_ms=8.0, survey_speed_ms=5.0, usable_energy_wh=480.0,
                      cruise_power_w=2200.0, survey_power_w=2400.0, turn_energy_wh=0.8,
                      takeoff_landing_wh=12.0, battery_swap_s=240.0, tank_l=tank_l,
                      application_rate_l_ha=rate_l_ha, spray_swath_m=swath_m)


@dataclass
class Sortie:
    index: int
    swaths: list[Swath]
    energy_wh: float
    duration_s: float
    distance_m: float
    liquid_l: float = 0.0


@dataclass
class MissionPlan:
    sorties: list[Sortie]
    home: tuple[float, float]
    drone: DroneModel
    solver_status: str = ""
    objective: float = 0.0
    treat_fraction: dict | None = None

    @property
    def n_sorties(self) -> int:
        return len(self.sorties)

    @property
    def total_energy_wh(self) -> float:
        return sum(s.energy_wh for s in self.sorties)

    @property
    def total_time_s(self) -> float:
        swaps = max(0, self.n_sorties - 1) * self.drone.battery_swap_s
        return sum(s.duration_s for s in self.sorties) + swaps

    @property
    def total_distance_m(self) -> float:
        return sum(s.distance_m for s in self.sorties)

    def summary(self) -> dict:
        return {
            "sorties": self.n_sorties,
            "total_energy_wh": round(self.total_energy_wh, 1),
            "total_time_min": round(self.total_time_s / 60.0, 1),
            "total_distance_m": round(self.total_distance_m, 1),
            "max_sortie_energy_wh": round(max((s.energy_wh for s in self.sorties), default=0.0), 1),
            "energy_budget_wh": self.drone.usable_energy_wh,
            "total_liquid_l": round(sum(s.liquid_l for s in self.sorties), 1),
            "tank_l": self.drone.tank_l,
            "mode": ("spot" if self.treat_fraction is not None else
                     "broadcast" if self.drone.tank_l else "survey"),
            "solver_status": self.solver_status,
        }


def _swath_energy(sw: Swath, d: DroneModel) -> float:
    return d.survey_wh(sw.length_m)


def plan_sorties(swaths: list[Swath], home: tuple[float, float],
                 drone: DroneModel | None = None, max_sorties: int | None = None,
                 time_limit_s: int = 10, first_solution: str = "PATH_CHEAPEST_ARC",
                 treat_fraction: dict[int, float] | None = None,
                 ) -> MissionPlan:
    """Partition swaths into energy- and tank-feasible sorties and order them.

    ``treat_fraction`` (swath index -> fraction 0..1) switches to spot-spray:
    liquid per swath scales with the fraction and swaths at 0 are not flown.
    Returns a plan whose every sortie satisfies the endurance and tank
    constraints, or raises if no such plan exists within ``max_sorties``.
    """
    d = drone or DroneModel()
    if treat_fraction is not None:
        swaths = [sw for sw in swaths if treat_fraction.get(sw.index, 0.0) > 0.0]
    if not swaths:
        return MissionPlan([], home, d, "EMPTY", treat_fraction=treat_fraction)
    frac = (lambda idx: float(treat_fraction.get(idx, 0.0))) if treat_fraction is not None \
        else (lambda idx: 1.0)

    # Node 0 is the depot. Each swath contributes two nodes: entering from its
    # own start, or entering reversed.
    nodes: list[tuple[float, float]] = [home]
    exits: list[tuple[float, float]] = [home]
    swath_of: list[int | None] = [None]
    oriented: list[Swath | None] = [None]
    for sw in swaths:
        for cand in (sw, sw.reversed()):
            nodes.append(cand.start)
            exits.append(cand.end)
            swath_of.append(sw.index)
            oriented.append(cand)
    n_nodes = len(nodes)

    # Lower bound on sorties from pure energy, then a little slack for transit.
    total_survey = sum(_swath_energy(sw, d) for sw in swaths)
    lb = max(1, math.ceil(total_survey / max(d.usable_energy_wh - d.takeoff_landing_wh, 1e-6)))
    total_liquid = sum(d.swath_liquid_l(sw.length_m, frac(sw.index)) for sw in swaths)
    if d.tank_l:
        biggest = max(d.swath_liquid_l(sw.length_m, frac(sw.index)) for sw in swaths)
        if biggest > d.tank_l:
            raise RuntimeError(f"one swath needs {biggest:.1f} L but the tank holds {d.tank_l:g} L; "
                               "split long swaths before planning")
        lb = max(lb, math.ceil(total_liquid / d.tank_l))
    n_veh = max_sorties or max(lb + 2, int(lb * 1.25) + 1, 2)

    mgr = pywrapcp.RoutingIndexManager(n_nodes, n_veh, 0)
    routing = pywrapcp.RoutingModel(mgr)

    def dist(from_idx, to_idx) -> float:
        i, j = mgr.IndexToNode(from_idx), mgr.IndexToNode(to_idx)
        return math.dist(exits[i], nodes[j])

    def energy_cb(from_idx, to_idx) -> int:
        """Arc energy: transit to the next swath plus flying that swath.

        Charged in milli-watt-hours because OR-Tools dimensions are integer.
        """
        j = mgr.IndexToNode(to_idx)
        wh = d.transit_wh(dist(from_idx, to_idx))
        if oriented[j] is not None:
            wh += _swath_energy(oriented[j], d)
            wh += d.turn_energy_wh
        else:
            wh += d.takeoff_landing_wh   # returning to the depot ends the sortie
        return int(round(wh * 1000))

    def time_cb(from_idx, to_idx) -> int:
        j = mgr.IndexToNode(to_idx)
        s = d.transit_s(dist(from_idx, to_idx))
        if oriented[j] is not None:
            s += d.survey_s(oriented[j].length_m)
        return int(round(s))

    energy_i = routing.RegisterTransitCallback(energy_cb)
    time_i = routing.RegisterTransitCallback(time_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(time_i)

    budget_mwh = int((d.usable_energy_wh - d.takeoff_landing_wh) * 1000)
    routing.AddDimension(energy_i, 0, budget_mwh, True, "Energy")
    routing.AddDimension(time_i, 0, 24 * 3600, True, "Time")

    if d.tank_l:
        def liquid_cb(from_idx) -> int:
            """Liquid sprayed on arriving at a swath node, in millilitres."""
            j = mgr.IndexToNode(from_idx)
            if oriented[j] is None:
                return 0
            return int(math.ceil(d.swath_liquid_l(oriented[j].length_m, frac(oriented[j].index)) * 1000))
        liquid_i = routing.RegisterUnaryTransitCallback(liquid_cb)
        routing.AddDimensionWithVehicleCapacity(
            liquid_i, 0, [int(d.tank_l * 1000)] * n_veh, True, "Liquid")

    # Exactly one orientation of each swath must be visited: make the two
    # orientation nodes a disjunction with cardinality 1 and no drop penalty.
    for k, _sw in enumerate(swaths):
        a = mgr.NodeToIndex(1 + 2 * k)
        b = mgr.NodeToIndex(1 + 2 * k + 1)
        routing.AddDisjunction([a, b], -1, 1)   # -1 penalty = must visit exactly one

    # Each additional sortie costs a battery swap, so the solver economises.
    for v in range(n_veh):
        routing.SetFixedCostOfVehicle(int(d.battery_swap_s), v)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = getattr(
        routing_enums_pb2.FirstSolutionStrategy, first_solution)
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)
    params.time_limit.FromSeconds(int(time_limit_s))

    sol = routing.SolveWithParameters(params)
    if sol is None:
        raise RuntimeError(
            f"no feasible mission within {n_veh} sorties for {len(swaths)} swaths; "
            f"energy budget {d.usable_energy_wh:.0f} Wh, survey demand "
            f"{total_survey:.0f} Wh. Increase max_sorties or the battery.")

    sorties: list[Sortie] = []
    for v in range(n_veh):
        idx = routing.Start(v)
        if routing.IsEnd(sol.Value(routing.NextVar(idx))):
            continue
        seq: list[Swath] = []
        energy = d.takeoff_landing_wh
        seconds = 0.0
        metres = 0.0
        liquid = 0.0
        while not routing.IsEnd(idx):
            nxt = sol.Value(routing.NextVar(idx))
            j = mgr.IndexToNode(nxt)
            leg = dist(idx, nxt)
            metres += leg
            energy += d.transit_wh(leg)
            seconds += d.transit_s(leg)
            if oriented[j] is not None:
                sw = oriented[j]
                seq.append(sw)
                metres += sw.length_m
                energy += _swath_energy(sw, d) + d.turn_energy_wh
                seconds += d.survey_s(sw.length_m)
                if d.tank_l:
                    liquid += d.swath_liquid_l(sw.length_m, frac(sw.index))
            idx = nxt
        if seq:
            sorties.append(Sortie(len(sorties), seq, energy, seconds, metres, liquid))

    return MissionPlan(sorties, home, d, sol and "OK" or "NONE",
                       float(sol.ObjectiveValue()), treat_fraction=treat_fraction)


def verify(plan: MissionPlan) -> list[str]:
    """Independent feasibility check of a produced plan.

    Never trust the solver's own accounting for a safety-relevant constraint --
    recompute it. This has caught real errors: a callback that charges energy
    on the wrong arc produces a plan the solver believes is feasible and which
    runs the battery flat on the second sortie.
    """
    problems: list[str] = []
    d = plan.drone
    seen: dict[int, int] = {}
    tf = plan.treat_fraction
    for s in plan.sorties:
        recomputed = d.takeoff_landing_wh
        prev = plan.home
        if d.tank_l:
            liters = sum(d.swath_liquid_l(sw.length_m, tf.get(sw.index, 0.0) if tf is not None else 1.0)
                         for sw in s.swaths)
            if liters > d.tank_l + 1e-6:
                problems.append(f"sortie {s.index} sprays {liters:.2f} L, tank is {d.tank_l:g} L")
        for sw in s.swaths:
            recomputed += d.transit_wh(math.dist(prev, sw.start))
            recomputed += d.survey_wh(sw.length_m) + d.turn_energy_wh
            prev = sw.end
            seen[sw.index] = seen.get(sw.index, 0) + 1
        recomputed += d.transit_wh(math.dist(prev, plan.home))
        if recomputed > d.usable_energy_wh + 1e-6:
            problems.append(
                f"sortie {s.index} needs {recomputed:.1f} Wh, budget is "
                f"{d.usable_energy_wh:.1f} Wh")
        if abs(recomputed - s.energy_wh) > 0.5 + 0.02 * recomputed:
            problems.append(
                f"sortie {s.index} energy accounting mismatch: reported "
                f"{s.energy_wh:.1f} Wh, recomputed {recomputed:.1f} Wh")
    dupes = [i for i, c in seen.items() if c != 1]
    if dupes:
        problems.append(f"swaths visited a number of times other than once: {dupes}")
    return problems
