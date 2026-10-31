"""Damage layer: potato late blight and weed competition, coupled to WOFOST.

WOFOST simulates a healthy crop limited by water (and nitrogen in 8.1). The
decisions Future Agri makes -- spray fungicide, spray herbicide -- only have
value if something can destroy yield, so this module adds the two biggest
biotic losses for the target crops. Ported from the 30 Oct 2026 audit
(``audit/damage.py``) with the same coefficients.

**Every coefficient here is a stated assumption from the general
plant-pathology and weed-science literature, not a calibration.** With these
defaults about 65 % of seasons at Bennett get blight and an unsprayed
susceptible crop is mostly destroyed. Fungicide and scouting verdicts are
driven by these numbers and by the prices in the planner, so they are exposed
here to be fitted from a field season, and every result built on them must say
so.

Late blight
-----------
Logistic epidemic on the fraction of leaf area destroyed, ``S``:
``dS/dt = r * S * (1 - S)`` with ``r`` high on favourable days (night humidity
near saturation or rain, mild temperatures -- a Smith/Hutton-style proxy) and
low otherwise. Infection starts after two consecutive favourable days, once
the crop is 30 days old, *if* inoculum arrives that season. A protectant spray
cuts ``r`` by its efficacy for 7 days and is washed off by >= 10 mm of rain.

Coupling: each day the newly destroyed fraction of the *remaining healthy*
canopy is removed from WOFOST's LAI with ``set_variable("LAI")``, which
rescales leaf biomass age classes, so lost canopy reduces photosynthesis and
therefore tuber growth from that day on.

Weeds
-----
Cousens' rectangular hyperbola for yield loss from early weed density:
``YL = I*d / (1 + I*d/A)`` (I = % loss per plant/m2 at low density, A = maximum
% loss). Applied to the final tuber or grain yield. A herbicide pass removes
``efficacy`` of the weeds before they compete.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np


# ---------------------------------------------------------------- late blight

@dataclass
class BlightParams:
    r_fav: float = 0.30          # logistic rate on favourable days (1/day)
    r_unfav: float = 0.03        # ... on unfavourable days
    s0: float = 1e-3             # severity at first infection
    p_inoculum: float = 0.6      # probability the pathogen arrives in a season
    earliest_day: int = 30       # days after planting before infection is possible
    protect_days: int = 7        # protectant residual
    efficacy: float = 0.85       # fraction of the infection rate suppressed while protected
    washoff_mm: float = 10.0     # rain that removes the protectant
    provenance: str = "literature-style assumptions (audit 30 Oct 2026); not calibrated"


def _es(t: float) -> float:
    return 6.108 * math.exp(17.27 * t / (t + 237.3))


def favourable(rec: dict) -> bool:
    """Blight-favourable day: wet canopy at night and mild temperatures.

    ``rec`` uses generator units (RAIN in mm, VAP in hPa, temperatures in C).
    Night humidity is approximated by actual vapour pressure over saturation at
    Tmin -- the dew-point proxy that station data supports.
    """
    tmean = 0.5 * (rec["TMAX"] + rec["TMIN"])
    rh_night = rec["VAP"] / _es(rec["TMIN"])
    wet = rec["RAIN"] >= 1.0 or rh_night >= 0.90
    return wet and rec["TMIN"] >= 7.0 and 8.0 <= tmean <= 24.0


def infection_day(records: list[dict], plant: dt.date, rng: np.random.Generator | None = None,
                  p: BlightParams | None = None, inoculum: bool | None = None,
                  start: dt.date | None = None) -> dt.date | None:
    """First day of two consecutive favourable days after ``earliest_day``.

    ``inoculum``: True/False when a regional alert says whether the pathogen is
    present; None draws it with probability ``p_inoculum``. ``start`` restricts
    the search to days on or after it (for forecasts issued mid-season).
    """
    p = p or BlightParams()
    if inoculum is None:
        rng = rng or np.random.default_rng()
        inoculum = bool(rng.random() < p.p_inoculum)
    if not inoculum:
        return None
    first = plant + dt.timedelta(days=p.earliest_day)
    if start is not None and start > first:
        first = start
    for i in range(1, len(records)):
        r = records[i]
        if r["DAY"] < first:
            continue
        if favourable(r) and favourable(records[i - 1]):
            return r["DAY"]
    return None


class Blight:
    """Daily late-blight epidemic state."""

    def __init__(self, inf_day: dt.date | None, p: BlightParams | None = None,
                 severity: float = 0.0):
        self.p = p or BlightParams()
        self.inf_day = inf_day
        self.S = float(severity)
        self.protected_until: dt.date | None = None

    def copy(self) -> "Blight":
        b = Blight(self.inf_day, self.p, self.S)
        b.protected_until = self.protected_until
        return b

    def spray(self, day: dt.date) -> None:
        self.protected_until = day + dt.timedelta(days=self.p.protect_days)

    def protected(self, day: dt.date) -> bool:
        return self.protected_until is not None and day <= self.protected_until

    def step(self, rec: dict) -> float:
        """Advance one day; return the newly destroyed leaf fraction dS."""
        d = rec["DAY"]
        if self.protected_until and rec["RAIN"] >= self.p.washoff_mm:
            self.protected_until = None
        if self.inf_day is None or d < self.inf_day:
            return 0.0
        if self.S == 0.0:
            self.S = self.p.s0
            return self.S
        r = self.p.r_fav if favourable(rec) else self.p.r_unfav
        if self.protected(d):
            r *= 1.0 - self.p.efficacy
        new = min(0.999, self.S + r * self.S * (1.0 - self.S))
        dS, self.S = new - self.S, new
        return dS


# ---------------------------------------------------------------------- weeds

@dataclass
class WeedParams:
    I: float = 1.2                    # % yield loss per weed/m2 at low density
    A: float = 55.0                   # maximum % yield loss
    herbicide_efficacy: float = 0.90
    provenance: str = "Cousens (1985) form; coefficients assumed (audit 30 Oct 2026)"


def weed_loss_frac(density_m2: float, p: WeedParams | None = None) -> float:
    """Fractional yield loss from early weed density (Cousens hyperbola)."""
    p = p or WeedParams()
    d = max(0.0, float(density_m2))
    return (p.I * d / (1.0 + p.I * d / p.A)) / 100.0


def economic_threshold(price_per_kg: float, yield_kg_ha: float, treatment_cost: float,
                       p: WeedParams | None = None) -> float:
    """Weed density (plants/m2) above which treatment pays for itself."""
    p = p or WeedParams()
    lo, hi = 0.0, 500.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        saved = (weed_loss_frac(mid, p) - weed_loss_frac(mid * (1 - p.herbicide_efficacy), p))
        if saved * yield_kg_ha * price_per_kg > treatment_cost:
            hi = mid
        else:
            lo = mid
    return hi


# ---------------------------------------------------------- coupled simulation

@dataclass
class DamageResult:
    TWSO_healthy_path: float          # tuber/grain yield with blight but before weed loss
    TWSO: float                       # final yield after blight and weeds
    sprays: list = field(default_factory=list)
    final_severity: float = 0.0
    weed_loss: float = 0.0
    irrigations: list = field(default_factory=list)


def run_with_damage(twin, records: list[dict], blight: Blight | None = None,
                    spray_days=(), policy=None, weed_density: float = 0.0,
                    herbicide: bool = False, weeds: WeedParams | None = None,
                    end: dt.date | None = None) -> DamageResult:
    """Run a ``ZoneTwin`` day by day with blight leaf loss and weed yield loss.

    ``policy(day, blight, twin, sprays) -> bool`` may decide to spray each day
    (used by the closed-loop simulations); ``spray_days`` are fixed sprays.
    """
    blight = blight or Blight(None)
    by_day = {r["DAY"]: r for r in records}
    sprays: list[dt.date] = []
    spray_set = set(spray_days)
    d = twin.cfg.sowing_date
    stop = end or twin.cfg.harvest_date
    m = twin.model
    while d <= stop and not m.flag_terminate:
        rec = by_day.get(d)
        if rec is None:
            break
        if d in spray_set or (policy is not None and policy(d, blight, twin, sprays)):
            blight.spray(d)
            sprays.append(d)
        dS = blight.step(rec)
        m.run(days=1)
        if m.flag_terminate:
            break
        lai = m.get_variable("LAI")
        if dS > 0 and lai:
            healthy = max(1e-3, 1.0 - blight.S + dS)
            m.set_variable("LAI", max(0.01, lai * (1.0 - dS / healthy)))
        d += dt.timedelta(days=1)
    if end is None:
        m.run_till_terminate()
    y = float((m.get_summary_output() or [{}])[0].get("TWSO") or 0.0) if end is None \
        else float(m.get_variable("TWSO") or 0.0)
    dens = weed_density * ((1 - (weeds or WeedParams()).herbicide_efficacy) if herbicide else 1.0)
    wl = weed_loss_frac(dens, weeds)
    return DamageResult(TWSO_healthy_path=y, TWSO=y * (1 - wl), sprays=sprays,
                        final_severity=blight.S, weed_loss=wl)
