"""Anchor the ground truth to what the real field actually did.

The field's Sentinel-2 cover series for the season is assimilated as canopy
cover into an ensemble of WOFOST runs (the project's EnKF with parameter
augmentation); the posterior TSUM1, TSUM2, SPAN and RGRLAI become the truth's
crop parameters. The virtual farm then grows on the real field's calendar --
emergence, canopy closure and senescence where the satellite saw them.
"""
from __future__ import annotations

import datetime as dt

from fa_twin.assimilation import EnsembleTwin, Observation
from fa_twin.engine import ZoneConfig, ZoneTwin
from fa_twin.weather import records_provider

K_COVER = 0.6


def _provider(cfg: ZoneConfig, records: list[dict], elev: float = 0.0):
    return records_provider(records, cfg.latitude, cfg.longitude, elev, "observed (anchor)")


def simulate_cover(cfg: ZoneConfig, records: list[dict], days: list[dt.date],
                   overrides: dict | None = None, engine: str = "Wofost72_WLP_CWB") -> list[float]:
    """Canopy cover of a deterministic run on the given days."""
    import math
    c = ZoneConfig(**{**cfg.__dict__, "param_overrides": {**cfg.param_overrides, **(overrides or {})}})
    zt = ZoneTwin(c, _provider(c, records), engine=engine)
    out = []
    for d in sorted(days):
        if not zt.terminated:
            zt.run_till(d)
        lai = 0.0 if zt.terminated else float(zt.model.get_variable("LAI") or 0.0)
        out.append(1.0 - math.exp(-K_COVER * lai))
    return out


def anchor_to_cover(cfg: ZoneConfig, records: list[dict], series: list[tuple], members: int = 24,
                    seed: int = 0, sd: float = 0.08, engine: str = "Wofost72_WLP_CWB",
                    min_days_after_sowing: int = 10) -> tuple[dict, dict]:
    """Fit crop parameters to an observed cover series [(date, ndvi|None, cover)].
    -> (parameter overrides, info)."""
    obs = [Observation(d, "CC", float(c), sd) for d, _, c in series
           if cfg.sowing_date + dt.timedelta(days=min_days_after_sowing) <= d <= cfg.harvest_date]
    info = {"n_obs": len(obs), "members": members, "obs_sd": sd}
    if len(obs) < 3:
        info["note"] = "fewer than 3 usable cover observations: shipped parameters kept"
        return {}, info
    et = EnsembleTwin(cfg, _provider(cfg, records), n_members=members, seed=seed, engine=engine)
    res = et.run(obs)
    post = et.posterior_parameters()
    info["rejected"] = int(sum(res.rejected))
    info["posterior"] = post
    return {k: float(v) for k, v in post.items()}, info
