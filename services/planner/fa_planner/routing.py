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

    def transit_wh(self, distance_m: float) -> float:
        return (distance_m / self.cruise_speed_ms) * self.cruise_power_w / 3600.0

    def survey_wh(self, distance_m: float) -> float:
        return (distance_m / self.survey_speed_ms) * self.survey_power_w / 3600.0

    def transit_s(self, distance_m: float) -> float:
        return distance_m / self.cruise_speed_ms

    def survey_s(self, distance_m: float) -> float:
        return distance_m / self.survey_speed_ms


@dataclass
class Sortie:
    index: int
    swaths: list[Swath]
    energy_wh: float
    duration_s: float
    distance_m: float


@dataclass
class MissionPlan:
    sorties: list[Sortie]
    home: tuple[float, float]
    drone: DroneModel
    solver_status: str = ""
    objective: float = 0.0

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
            "solver_status": self.solver_status,
        }


def _swath_energy(sw: Swath, d: DroneModel) -> float:
    return d.survey_wh(sw.length_m)


def plan_sorties(swaths: list[Swath], home: tuple[float, float],
                 drone: DroneModel | None = None, max_sorties: int | None = None,
                 time_limit_s: int = 10, first_solution: str = "PATH_CHEAPEST_ARC",
                 ) -> MissionPlan:
    """Partition swaths into energy-feasible sorties and order them.

    Returns a plan whose every sortie satisfies the endurance constraint, or
    raises if no such plan exists within ``max_sorties``.
    """
    d = drone or DroneModel()
    if not swaths:
        return MissionPlan([], home, d, "EMPTY")

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
    n_veh = max_sorties or max(lb + 2, 2)

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
            idx = nxt
        if seq:
            sorties.append(Sortie(len(sorties), seq, energy, seconds, metres))

    return MissionPlan(sorties, home, d, sol and "OK" or "NONE",
                       float(sol.ObjectiveValue()))


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
    for s in plan.sorties:
        recomputed = d.takeoff_landing_wh
        prev = plan.home
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
