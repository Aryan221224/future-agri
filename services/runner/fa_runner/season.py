"""One closed-loop season on a virtual farm.

Each morning: the truth has run through yesterday; the twin receives yesterday's
observed weather. On the scan day the separate camera drone scans the truth and
the twin ingests the DigitalFarm (EnKF on cover and stage; herbicide from the
weed map). Weekly from 4 weeks after sowing the twin plans fungicide with the
real ensemble forecast issued that day (``ForecastFeed``), scouting with a
coarser camera flight when the plan is undecided. Every spray is flown by the
drone twin on the first day whose *observed* weather passes the spray window;
what it actually deposited is applied to the truth. At harvest the truth's
yield and every cost give the season's net return.

Policies
--------
``none``        no sprays (value-of-protection baseline)
``calendar``    blanket herbicide 30 days after sowing; fungicide every 10 days
                from day 30 (potato) -- the conventional schedule
``twin``        scan-driven herbicide (none/spot/blanket) and forecast-planned
                fungicide with event-triggered scouting
``twin_alert``  ``twin`` plus a regional inoculum alert (whether the pathogen arrived)
"""
from __future__ import annotations

from fa_drone.mission import MissionSettings
from fa_drone.spec import DroneSpec

from .scenarios import ForecastFeed
from .session import MAX_WAIT_DAYS, POLICIES, SeasonResult, SeasonSession  # noqa: F401  (re-exported)


def run_season(vf, policy: str, seed: int = 0, feed: ForecastFeed | None = None,
               spec: DroneSpec | None = None, flight: MissionSettings | None = None,
               scan_das: int = 45, scan_gsd_m: float = 0.005, camera: str = "p4rtk",
               scout_gsd_m: float = 0.01, plan_members: int = 10, objective: str = "mean",
               cache=None, expected_yield_kg_ha: float | None = None) -> SeasonResult:
    """A whole season with every proposal approved (the batch path of ``SeasonSession``)."""
    s = SeasonSession(vf, policy, seed=seed, feed=feed, spec=spec, flight=flight, scan_das=scan_das,
                      scan_gsd_m=scan_gsd_m, camera=camera, scout_gsd_m=scout_gsd_m,
                      plan_members=plan_members, objective=objective, cache=cache,
                      expected_yield_kg_ha=expected_yield_kg_ha, approvals="auto", trace=False)
    s.run_until()
    return s.finish()
