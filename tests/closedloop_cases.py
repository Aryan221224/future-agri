"""Shared set-up for the closed-loop tests: the synthetic 1 ha farm with enough weeds that
the twin proposes a spot herbicide job after its first scan."""
import dataclasses

import numpy as np

from fa_runner.session import SeasonSession
from test_session import _kwargs

WEEDS_M2 = 25.0          # at 6/m2 (the default) the twin decides "none"; at 25 it proposes spot


def herb_kwargs() -> dict:
    kw = _kwargs(inoculum=False)
    kw["vf"].spec = dataclasses.replace(kw["vf"].spec, weed_mean_m2=WEEDS_M2)
    return kw


def session_with_herbicide_op():
    """(session paused on the day the herbicide job is proposed, that job)."""
    s = SeasonSession(**herb_kwargs())
    while not s.done:
        s.run_until(stop_on_decision=True)
        ops = [o for o in s.ops.values() if o.status == "proposed"]
        herb = [o for o in ops if o.product == "herbicide"]
        if herb:
            return s, herb[0]
        for o in ops:
            s.approve(o.id)
    raise AssertionError("no herbicide job proposed")


def session_on_flyable_day():
    """(session on the first day the proposed herbicide job can be flown, that job still
    proposed). Flying it runs that day's morning first, as fly_now does."""
    from fa_runner.scenarios import flight_record
    s, op = session_with_herbicide_op()
    for _ in range(40):
        rec = s.world.weather(s.day)
        if rec is not None and s.window.ok(flight_record(rec)) and op.status == "proposed":
            return s, op
        s.step()
    raise AssertionError("no flyable day within 20 days")


def clone(s: SeasonSession) -> SeasonSession:
    return SeasonSession.restore(herb_kwargs(), s.actions)


def half_mask(grid) -> np.ndarray:
    m = np.zeros(grid.n_cells, bool)
    m[: grid.n_cells // 2] = True
    return m


def first_quarter(grid) -> np.ndarray:
    m = np.zeros(grid.n_cells, bool)
    m[: grid.n_cells // 4] = True
    return m


def partial_obs(s: SeasonSession, observed: np.ndarray, weeds_m2: float):
    """A scan of today that saw ``weeds_m2`` everywhere it looked, and nothing elsewhere."""
    obs = s.world.scan(s.day, s.scan_gsd_m, s.camera, np.random.default_rng(5), s.archetype.crop)
    est = np.where(observed, weeds_m2, np.nan)
    sd = np.where(observed, 0.5, np.inf)
    return dataclasses.replace(obs, weed_density_est=est, weed_density_sd=sd, observed=observed)
