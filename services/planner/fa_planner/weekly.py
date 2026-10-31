"""Weekly rolling-horizon planner: spray fungicide, spray herbicide, scout, or wait.

Every week the twin re-plans from what it knows now (observed weather so far,
any scouted blight severity, protectant status, the regional inoculum alert)
over a forecast ensemble: days 1-15 forecast, then climatology. Each candidate
is scored on the *same* ensemble members (common random numbers) in Rs/ha.

Why weekly
----------
In the 30 Oct 2026 audit (S8, 40 seasons at Bennett, 28 with blight):

* weekly forecast-only twin vs a 10-day calendar: **-2.1 sprays (95 % CI -2.5
  to -1.7, -26 %)** at net +Rs 680/ha (CI -718 to +1,947: not significant);
* re-planning **monthly** missed epidemics and lost ~Rs 21,000/ha vs weekly;
* weekly drone scouting cost more than it saved (-Rs 2,561/ha vs calendar),
  so scouting here is **event-triggered**, not routine;
* most of the remaining value of perfect information (Rs 5,196/ha) came from
  seasons with no infection at all, where the twin still sprayed -- hence the
  **inoculum alert** input.

All of these depend on the blight and price assumptions in ``fa_twin.damage``
and ``PotatoEconomics``. Change them and the verdicts can change.

Planning model
--------------
Fast by design: per member, the crop's daily tuber-growth path comes from one
WOFOST run on that member's weather (``growth_fn``), the epidemic is simulated
from today's state, and the loss is ``sum(daily tuber growth x severity)``.
That is the audit's planning model; the truth simulation couples blight to the
crop day by day.

Objective: risk-neutral (mean net) by default; ``objective="cvar"`` scores the
mean of the worst 20 % of members instead. The audit found risk-neutral used
fewer sprays for the same net with this blight model.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from fa_twin.damage import (Blight, BlightParams, WeedParams, economic_threshold, favourable,
                            weed_loss_frac)

from .sop import DEFAULT_WINDOW, SprayWindow, consensus_window


@dataclass
class PotatoEconomics:
    """Prices and costs in Rs/ha. All are stated assumptions -- replace locally."""
    price_per_kg_fresh: float = 10.0
    dry_matter: float = 0.20
    #: WOFOST's water-limited ceiling is ~45-50 t/ha fresh; typical farm yields
    #: are about half (nitrogen, pests, management). Revenue is scaled to farm
    #: level so a loss of x % of the ceiling is valued as x % of a farm crop.
    farm_factor: float = 0.5
    fungicide_per_spray: float = 1500.0       # drone application incl. product
    scouting_per_flight: float = 300.0
    herbicide_blanket: float = 2000.0
    herbicide_spot_fixed: float = 500.0
    herbicide_spot_per_fraction: float = 1500.0
    irrigation_per_cm: float = 300.0
    provenance: str = "assumptions (audit 30 Oct 2026): potato Rs10/kg, spray Rs1500, scout Rs300"

    def revenue(self, dry_kg_ha: float) -> float:
        return self.price_per_kg_fresh * self.farm_factor * dry_kg_ha / self.dry_matter


#: Blight severity (fraction of leaf area destroyed) a scouting flight can see.
DETECTION_LIMIT = 0.005


@dataclass
class FieldStatus:
    """What the twin knows on the planning day."""
    day: dt.date
    plant: dt.date
    harvest: dt.date
    observed_severity: float | None = None    # legacy: a scouted severity *today*
    protected_until: dt.date | None = None    # protectant still active until
    inoculum: bool | None = None              # regional alert: True / False / unknown
    #: every scouting flight so far: (day, severity) or (day, None) = not seen
    scouting: list = field(default_factory=list)
    #: every fungicide spray so far
    sprays: list = field(default_factory=list)


@dataclass
class FungicideDecision:
    spray_day: dt.date | None
    expected_gain: float                       # Rs/ha, spray minus wait, on the objective
    p_spray_better: float                      # fraction of members where spraying wins
    values: dict = field(default_factory=dict) # candidate -> per-member net (Rs/ha, relative)
    reason: str = ""


def cvar_low(v, q: float = 0.2) -> float:
    v = np.sort(np.asarray(v, float))
    k = max(1, int(math.ceil(q * len(v))))
    return float(v[:k].mean())


def member_blight_state(recs: list[dict], status: FieldStatus, rng: np.random.Generator,
                        p: BlightParams | None = None, trace: bool = False,
                        obs_logsd: float = 0.3) -> Blight:
    """The epidemic as this member believes it stands at the start of ``status.day``.

    Replays the *observed* days from planting with the recorded sprays (and rain
    wash-off), so the planner starts from what has already happened instead of
    from nothing:

    * the latest positive scouting result sets the severity on its day (with
      retrieval noise) and the epidemic is propagated forward from there;
    * with no detection, an infection may already have happened unseen: its date
      is drawn from the observed weather by the same favourable-spell rule
      (if inoculum is present -- alert, or ``p_inoculum``);
    * a negative scouting result (``(day, None)``) is information: an infection
      that would have been visible that day is rejected and the search restarts
      after it.
    """
    p = p or BlightParams()
    today = status.day
    by_day = {r["DAY"]: r for r in recs}
    sprays = set(status.sprays or [])
    scouts = sorted(status.scouting or [], key=lambda x: x[0])
    if status.observed_severity:
        scouts = scouts + [(today, float(status.observed_severity))]
    positives = [(d, sv) for d, sv in scouts if sv is not None and sv >= DETECTION_LIMIT]
    negatives = [d for d, sv in scouts if sv is None or sv < DETECTION_LIMIT]

    def replay(inf_day, anchor=None):
        b = Blight(inf_day, p)
        tr = {}
        d = status.plant
        while d < today:
            r = by_day.get(d)
            if r is None:
                break
            if d in sprays:
                b.spray(d)
            if anchor is not None and d == anchor[0]:
                b.inf_day = b.inf_day or d
                b.S = anchor[1]
            b.step(r)
            tr[d] = b.S
            d += dt.timedelta(days=1)
        return b, tr

    if positives:
        d_obs, s_obs = positives[-1]
        s0 = float(min(0.99, s_obs * math.exp(rng.normal(0, obs_logsd))))
        b, tr = replay(d_obs, anchor=(d_obs, s0))
    else:
        inoc = status.inoculum
        if inoc is None:
            inoc = bool(rng.random() < p.p_inoculum)
        b, tr = replay(None)
        if inoc:
            search_from = status.plant + dt.timedelta(days=p.earliest_day)
            for _ in range(50):
                inf = None
                for i in range(1, len(recs)):
                    if recs[i]["DAY"] >= search_from and favourable(recs[i]) and favourable(recs[i - 1]):
                        inf = recs[i]["DAY"]
                        break
                if inf is None or inf >= today:
                    b, tr = replay(None)
                    b.inf_day = inf          # future infection, to be simulated by the planner
                    break
                b, tr = replay(inf)
                seen = [dn for dn in negatives if dn >= inf and tr.get(dn, 0.0) >= DETECTION_LIMIT]
                if not seen:
                    break
                search_from = seen[0] + dt.timedelta(days=1)
    if trace:
        b.trace = tr
    return b


def _plan_loss(recs: list[dict], growth: dict, start: Blight, d0: dt.date, harvest: dt.date,
               spray_day: dt.date | None) -> float:
    """Tuber dry-matter loss (kg/ha) from ``d0`` to harvest for one member/candidate."""
    b = start.copy()
    loss = 0.0
    for r in recs:
        d = r["DAY"]
        if d < d0 or d > harvest:
            continue
        if spray_day is not None and d == spray_day:
            b.spray(d)
        b.step(r)
        loss += growth.get(d, 0.0) * b.S
    return loss


def fungicide_decision(members: list[list[dict]], status: FieldStatus,
                       growth_fn: Callable[[list[dict]], dict], econ: PotatoEconomics | None = None,
                       blight: BlightParams | None = None, objective: str = "mean",
                       window: SprayWindow = DEFAULT_WINDOW, seed: int = 0,
                       severity_obs_logsd: float = 0.3) -> FungicideDecision:
    """Spray this week or wait, scored on one forecast ensemble with CRN."""
    econ = econ or PotatoEconomics()
    p = blight or BlightParams()
    day = consensus_window(members, status.day, 7, window=window)
    if day is None:
        return FungicideDecision(None, 0.0, 0.0, {}, "no sprayable day forecast this week")
    if status.protected_until and status.protected_until >= day + dt.timedelta(days=3):
        return FungicideDecision(None, 0.0, 0.0, {},
                                 f"protectant active until {status.protected_until}")
    rng = np.random.default_rng(seed)
    nets = {"wait": [], "spray": []}
    for recs in members:
        growth = growth_fn(recs)
        start = member_blight_state(recs, status, rng, p, obs_logsd=severity_obs_logsd)
        if status.protected_until and (start.protected_until is None
                                       or status.protected_until > start.protected_until):
            start.protected_until = status.protected_until
        for cand, sd in (("wait", None), ("spray", day)):
            loss = _plan_loss(recs, growth, start, status.day, status.harvest, sd)
            nets[cand].append(-econ.revenue(loss) - (econ.fungicide_per_spray if sd else 0.0))
    agg = cvar_low if objective == "cvar" else (lambda v: float(np.mean(v)))
    gain = agg(nets["spray"]) - agg(nets["wait"])
    p_better = float(np.mean(np.array(nets["spray"]) > np.array(nets["wait"])))
    spray = gain > 0
    reason = (f"{'spray' if spray else 'wait'}: expected {objective} gain Rs {gain:,.0f}/ha "
              f"(spray better in {p_better:.0%} of {len(members)} members)")
    return FungicideDecision(day if spray else None, float(gain), p_better,
                             {k: list(map(float, v)) for k, v in nets.items()}, reason)


def scouting_trigger(members: list[list[dict]], status: FieldStatus,
                     blight: BlightParams | None = None, horizon_days: int = 7,
                     threshold: float = 0.5, already_detected: bool = False,
                     p_spray_better: float | None = None, last_scouted: dt.date | None = None,
                     min_interval_days: int = 7,
                     undecided: tuple[float, float] = (0.25, 0.75)) -> tuple[bool, str]:
    """Fly a scouting mission only when what it could see would change the decision.

    Routine weekly scouting lost money in the audit (-Rs 2,561/ha vs calendar).
    A flight is worth its cost only if its observation can flip spray/wait, so
    scout when (a) infection is plausible (inoculum not ruled out, crop old
    enough, at least ``threshold`` of members show a two-day favourable spell),
    (b) blight has not already been confirmed, and (c) the planner is
    *undecided*: spraying wins in between ``undecided`` of members. When the
    planner is already confident either way, a flight buys nothing. After a
    detection, re-scouting is allowed (at most every ``min_interval_days``) so
    the severity the planner propagates is refreshed rather than going stale.
    """
    p = blight or BlightParams()
    if p_spray_better is None:
        return False, ("no spray decision this week to change (no sprayable day, or the "
                       "protectant is still active)")
    if already_detected:
        return False, "blight already confirmed; no new information from scouting"
    if last_scouted is not None and (status.day - last_scouted).days < min_interval_days:
        return False, f"scouted {(status.day - last_scouted).days} d ago"
    if status.inoculum is False:
        return False, "regional alert: no inoculum"
    if (status.day - status.plant).days < p.earliest_day:
        return False, "crop too young for infection"
    end = status.day + dt.timedelta(days=horizon_days)
    hits = 0
    for recs in members:
        wk = [r for r in recs if status.day <= r["DAY"] < end]
        hits += any(favourable(a) and favourable(b) for a, b in zip(wk, wk[1:]))
    frac = hits / max(1, len(members))
    if frac < threshold:
        return False, f"low risk: {frac:.0%} of members show a favourable spell this week"
    if p_spray_better is not None and not (undecided[0] < p_spray_better < undecided[1]):
        return False, (f"risk {frac:.0%} but planner already decided "
                       f"(spray better in {p_spray_better:.0%} of members)")
    return True, f"{frac:.0%} of members show a favourable spell and the spray call is close"


@dataclass
class HerbicideDecision:
    option: str                      # "none" | "blanket" | "spot"
    treated_fraction: float
    nets: dict
    threshold_m2: float
    reason: str


def herbicide_decision(density_cells: np.ndarray, expected_dry_yield: float,
                       econ: PotatoEconomics | None = None, weeds: WeedParams | None = None
                       ) -> HerbicideDecision:
    """None / blanket / spot spray from a sampled weed-density map (plants/m2 per cell).

    The audit found spot ~= blanket at typical densities (12 weeds/m2 median,
    threshold ~1.1/m2, so ~99 % of cells get treated): spot spraying saves
    herbicide only on low-density fields. This function makes that call per
    field instead of assuming it.
    """
    econ = econ or PotatoEconomics()
    w = weeds or WeedParams()
    dens = np.asarray(density_cells, float)
    base_rev = econ.revenue(expected_dry_yield)
    # Density at which treating one cell pays for itself, on the full Cousens
    # curve (not its low-density slope): revenue per kg dry x expected yield.
    thr = economic_threshold(econ.revenue(1.0), expected_dry_yield, econ.herbicide_blanket, w)

    def net(mask, cost):
        d = np.where(mask, dens * (1 - w.herbicide_efficacy), dens)
        loss = float(np.mean([weed_loss_frac(x, w) for x in d]))
        return base_rev * (1 - loss) - cost

    spot_mask = dens > thr
    frac = float(spot_mask.mean())
    nets = {"none": net(np.zeros(len(dens), bool), 0.0),
            "blanket": net(np.ones(len(dens), bool), econ.herbicide_blanket),
            "spot": net(spot_mask, econ.herbicide_spot_fixed + econ.herbicide_spot_per_fraction * frac)}
    best = max(nets, key=nets.get)
    return HerbicideDecision(best, frac if best == "spot" else (1.0 if best == "blanket" else 0.0),
                             nets, float(thr),
                             f"{best}: median {np.median(dens):.1f} weeds/m2, threshold {thr:.1f}/m2, "
                             f"{frac:.0%} of cells above it")
