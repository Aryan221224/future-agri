"""The remaining job after each sortie.

The planner's own rules re-run on the updated weed map (herbicide: none / blanket / spot;
fungicide stays field-level, so its original targets), minus the cells already dosed to at
least 90 % of the prescribed rate. Under-dosed cells of the flown area (skipped strips)
therefore come back by themselves.
"""
from __future__ import annotations

import numpy as np

from .prediction import DONE_SHARE


def merge_observations(prior: np.ndarray, prior_sd: np.ndarray, seen_cum: np.ndarray, est: np.ndarray,
                       sd: np.ndarray, seen: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(weed map, its sd, cells seen so far). The first sprayer look at a cell replaces the
    survey-scan prior (newer, closer); a second look is averaged by inverse variance. Unseen
    cells and uninformative looks (infinite sd) keep what was there."""
    prior, prior_sd = np.asarray(prior, float), np.asarray(prior_sd, float)
    est, sd = np.asarray(est, float), np.asarray(sd, float)
    ok = np.asarray(seen, bool) & np.isfinite(est) & np.isfinite(sd) & (sd > 0)
    again = ok & np.asarray(seen_cum, bool) & np.isfinite(prior_sd) & (prior_sd > 0)
    first = ok & ~again
    m, s = prior.copy(), prior_sd.copy()
    m[first], s[first] = est[first], sd[first]
    w0, w1 = 1.0 / prior_sd[again] ** 2, 1.0 / sd[again] ** 2
    m[again] = (w0 * prior[again] + w1 * est[again]) / (w0 + w1)
    s[again] = 1.0 / np.sqrt(w0 + w1)
    return m, s, np.asarray(seen_cum, bool) | ok


def remaining_targets(twin, weed_map: np.ndarray, dose_cum: np.ndarray, rate_l_ha: float, product: str,
                      original: np.ndarray | None = None) -> tuple[np.ndarray, str]:
    dose_cum = np.asarray(dose_cum, float)
    done = dose_cum >= DONE_SHARE * rate_l_ha - 1e-9
    if product == "herbicide":
        option, want = twin.herbicide_targets(weed_map)
        rule = f"{option} herbicide on the updated weed map ({int(want.sum())} cells need it)"
    else:
        want = np.ones(len(dose_cum), bool) if original is None else np.asarray(original, bool)
        rule = f"{product}: the approved targets ({int(want.sum())} cells; field-level decision)"
    under = want & ~done & (dose_cum > 0)
    tg = want & ~done
    return tg, (f"{rule}; {int((want & done).sum())} cells done (>= {DONE_SHARE:.0%} of the rate), "
                f"{int(under.sum())} under-dosed come back; {int(tg.sum())} cells remain")
