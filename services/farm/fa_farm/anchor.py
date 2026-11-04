"""Anchor the ground truth to what the real field actually did.

The field's Sentinel-2 cover series for the season is assimilated as canopy
cover into an ensemble of WOFOST runs (the project's EnKF with parameter
augmentation); the posterior TSUM1, TSUM2, SPAN and RGRLAI become the truth's
crop parameters. The virtual farm then grows on the real field's calendar --
emergence, canopy closure and senescence where the satellite saw them.

``canopy_size=True`` also fits how BIG the canopy gets (LEAFSHARE, a multiplier on the
leaf share of new growth, stems taking the complement), returned as FLTB/FSTB overrides.
Timing alone cannot correct a canopy that is too small or too large -- a thin stand, a
poor emergence, a field that grew more leaf than the variety's tables say.
"""
from __future__ import annotations

import datetime as dt

from fa_twin.assimilation import EnsembleTwin, Observation
from fa_twin.engine import ZoneConfig, ZoneTwin
from fa_twin.weather import records_provider

K_COVER = 0.6


def _provider(cfg: ZoneConfig, records: list[dict], elev: float = 0.0, soil_evap_factor: float | None = None):
    f = getattr(cfg, "soil_evap_factor", 1.0) if soil_evap_factor is None else soil_evap_factor
    return records_provider(records, cfg.latitude, cfg.longitude, elev, "observed (anchor)", soil_evap_factor=f)


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


def usable_cover(series: list[tuple], drop_below: float = 0.25) -> list[tuple]:
    """Observations up to the end of the crop's own canopy: once cover has peaked
    and then fallen below ``drop_below``, later rises are a cover crop, weeds or
    the next crop -- not this crop -- and are dropped."""
    out, peak, low, prev = [], 0.0, False, None
    for d, nd, c in sorted(series, key=lambda x: x[0]):
        if low and prev is not None and c > prev + 0.05:
            break                          # cover rising again after senescence
        out.append((d, nd, c))
        peak = max(peak, c)
        if peak >= 0.5 and c < drop_below:
            low = True
        prev = c
    return out


#: The software's satellite anchor (v5): satellite FVC operator (k fitted on training seasons),
#: canopy size fitted as well as timing, parameters only.
SATELLITE = {"operator": "FVC", "canopy_size": True, "update_state": False}


def anchor_to_cover(cfg: ZoneConfig, records: list[dict], series: list[tuple], members: int = 24,
                    seed: int = 0, sd: float = 0.08, engine: str = "Wofost72_WLP_CWB",
                    min_days_after_sowing: int = 10, soil_evap_factor: float | None = None,
                    canopy_size: bool = False, operator: str = "CC",
                    update_state: bool = True, events: list[dict] | None = None) -> tuple[dict, dict]:
    """Fit crop parameters to an observed cover series [(date, ndvi|None, cover)].
    -> (parameter overrides, info)."""
    obs = [Observation(d, operator, float(c), sd) for d, _, c in usable_cover(series)
           if cfg.sowing_date + dt.timedelta(days=min_days_after_sowing) <= d <= cfg.harvest_date]
    info = {"n_obs": len(obs), "members": members, "obs_sd": sd, "operator": operator,
            "canopy_size": canopy_size, "update_state": update_state}
    if len(obs) < 3:
        info["note"] = "fewer than 3 usable cover observations: shipped parameters kept"
        return {}, info
    et = EnsembleTwin(cfg, _provider(cfg, records, soil_evap_factor=soil_evap_factor), n_members=members, seed=seed,
                      engine=engine, canopy_size=canopy_size, update_state=update_state, events=events)
    res = et.run(obs)
    post = et.posterior_parameters()
    info["rejected"] = int(sum(res.rejected))
    info["posterior"] = post
    out = {k: float(v) for k, v in post.items() if k != "LEAFSHARE"}
    if "LEAFSHARE" in post:
        from fa_twin.management import leaf_share_overrides
        pp = ZoneTwin(cfg, _provider(cfg, records), engine=engine).model.parameterprovider
        out.update(leaf_share_overrides({"FLTB": list(pp["FLTB"]), "FSTB": list(pp["FSTB"])}, post["LEAFSHARE"]))
        info["leaf_share"] = float(post["LEAFSHARE"])
    return out, info
