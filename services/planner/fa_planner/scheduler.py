"""Agronomic scheduling: when to apply what, under uncertainty.

Why this is not MPPI
--------------------
An earlier revision of this architecture specified MPPI (model predictive path
integral control) for agronomic scheduling. That was wrong, and the reasoning is
worth writing down because MPPI is exactly the kind of choice that survives
review by sounding sophisticated.

MPPI's update is a softmax-weighted **arithmetic mean of sampled control
sequences**. It works in continuous control because the average of two good
steering angles is a good steering angle. Here the decision is "apply 0, 40, 80
or 120 kg N/ha in week k, or nothing", over ~20 weekly epochs of which almost
all are "nothing". Three structural mismatches follow:

1. The average of *urea* and *no application* is not an action. Relaxing to a
   simplex and rounding breaks the importance-sampling weights the derivation
   depends on, leaving a hand-tuned heuristic in an information-theoretic
   costume.
2. Gaussian perturbation around a nominal sequence is a poor proposal when the
   optimum is sparse -- act in weeks 4 and 11, do nothing in the other 18.
3. MPPI's exponential weighting assumes the rollout model is right and only the
   control is noisy. Here the dominant error is crop-model structural error and
   weather, which is a *scenario* problem, not a control-noise problem.

And there is no real-time constraint to justify an anytime sampling method: 20
decisions per season, not 20 per second.

What the benchmark actually found
---------------------------------
``bench/planner_comparison.py`` runs all of these on the real crop model. The
result is more nuanced than the argument above predicts, and the code should say
so rather than quietly claim a win:

* MPPI did **not** lose. On an N-limited paddy scenario it found the same
  optimum as beam search (CVaR 9034/ha, one 30 kg N application) for the same
  rollout budget. The structural objections stand -- the rounding step voids the
  importance-sampling weights, and the method has no way to express "at most 3
  applications" -- but on this problem size they did not bite.
* **Greedy found that same optimum using 65 rollouts against beam-5's 230.**
  Search width bought nothing. That is the finding that should drive the
  implementation: ship greedy, keep beam for the ranked alternatives an
  agronomist wants to see, and treat MPPI as a reference implementation.
* The naive agronomic heuristic -- split the season's N evenly across every
  epoch -- scored **worse than doing nothing** (CVaR 3484 against 7266) despite a
  higher *mean* margin (17752 against 7780). Four passes of fixed cost, paid in
  every scenario, against a yield gain that fails in the bad ones. This is the
  clearest argument in the whole system for optimising CVaR rather than the
  mean, and it is why the default objective is CVaR.

What is here instead
--------------------
``beam_search`` -- enumerate candidate actions per decision epoch, roll each out
through the crop twin over a scenario ensemble, keep the best K partial
schedules. Debuggable, explainable to an agronomist, and it returns a *ranked
list* of alternatives, which is what a farm manager actually wants.

``mppi_schedule`` is implemented too, honestly and as well as it can be, so the
comparison in ``bench/planner_comparison.py`` is an experiment rather than an
assertion.

Risk
----
The objective is CVaR, not the mean. CVaR at level alpha has a closed-form
linear representation (Rockafellar-Uryasev) and needs no risk library. We use
alpha ~ 0.8, deliberately not min-max robustness: worst-case optimisation over
an adversarial weather set produces schedules that assume it hails every week,
which no farmer will follow.
"""
from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Action:
    """One candidate intervention at one decision epoch."""
    kind: str                # "none" | "fertilise" | "irrigate"
    amount: float = 0.0
    product: str = ""
    cost_per_ha: float = 0.0

    def __str__(self) -> str:
        if self.kind == "none":
            return "-"
        return f"{self.kind[:4]}:{self.amount:g}"


NO_ACTION = Action("none")


@dataclass
class Economics:
    """Prices. The system must produce a defensible per-acre number.

    Half of farmers globally will not pay for advisory software at all, and
    those who will demand roughly 3:1 return. An architecture that cannot state
    its own economic effect is not going to clear that bar, so the objective is
    denominated in currency, never in kg of biomass.
    """
    # Indian paddy, 2024-25 order of magnitude, all in INR. These are inputs to
    # a decision, so they are stated explicitly and versioned rather than buried
    # as literals: change them per market and per season, and the recommendation
    # changes with them, which is the correct behaviour.
    currency: str = "INR"
    crop_price_per_kg: float = 23.0                # paddy, MSP-anchored
    n_cost_per_kg: float = 45.0                    # urea, per kg of N
    irrigation_cost_per_cm_ha: float = 300.0
    application_fixed_cost_per_ha: float = 500.0   # the drone pass itself


@dataclass
class Schedule:
    actions: dict[date, Action]
    expected_value: float = 0.0
    cvar: float = 0.0
    mean_yield: float = 0.0
    yield_p05: float = 0.0
    cost: float = 0.0
    n_applications: int = 0
    rationale: str = ""

    def as_events(self) -> list[dict]:
        """Convert to PCSE timed events."""
        out = []
        for d, a in sorted(self.actions.items()):
            if a.kind == "fertilise" and a.amount > 0:
                out.append({"date": d, "N_amount": float(a.amount)})
            elif a.kind == "irrigate" and a.amount > 0:
                out.append({"date": d, "irrigation_cm": float(a.amount)})
        return out


def cvar(values: np.ndarray, alpha: float = 0.8) -> float:
    """Conditional value at risk of the *lower* tail (we maximise value).

    CVaR_alpha here is the mean of the worst (1 - alpha) fraction of outcomes,
    which is what "how bad is a bad season under this plan" means.
    """
    v = np.sort(np.asarray(values, float))
    k = max(1, int(math.floor((1 - alpha) * len(v))))
    return float(v[:k].mean())


class ScenarioEvaluator:
    """Evaluates a schedule across a weather/parameter scenario ensemble.

    Scenarios are the honest representation of what we do not know. Each is a
    (weather, parameter-perturbation) draw; a schedule is scored by the
    distribution of net margins it produces across them.
    """

    def __init__(self, cfg, weathers: list, econ: Economics | None = None,
                 param_spread: float = 0.15, seed: int = 0, engine: str = "auto"):
        self.cfg = cfg
        self.weathers = list(weathers)
        self.econ = econ or Economics()
        self.engine = engine
        self.rng = np.random.default_rng(seed)
        self.param_spread = param_spread
        self._factors = [
            {p: float(np.exp(self.rng.normal(0, param_spread)))
             for p in ("TSUM1", "TSUM2", "SPAN", "RGRLAI")}
            for _ in self.weathers
        ]
        self._cache: dict[tuple, np.ndarray] = {}
        self.n_rollouts = 0

    def _key(self, events: list[dict]) -> tuple:
        return tuple(sorted((str(e["date"]), round(float(e.get("N_amount", 0.0)), 3),
                             round(float(e.get("irrigation_cm", 0.0)), 3))
                            for e in events))

    def yields(self, events: list[dict]) -> np.ndarray:
        """Yield across all scenarios for one schedule. Memoised."""
        from fa_twin.engine import ZoneTwin

        key = self._key(events)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out = []
        for w, fac in zip(self.weathers, self._factors):
            cfg = copy.deepcopy(self.cfg)
            try:
                tw = ZoneTwin(cfg, w, events=events, engine=self.engine)
                pp = tw.model.parameterprovider
                for p, f in fac.items():
                    try:
                        pp.set_override(p, float(pp[p]) * f, check=False)
                    except Exception:
                        pass
                tw.run_to_end()
                out.append(float(tw.summary().get("TWSO") or 0.0))
            except Exception as e:
                log.warning("scenario rollout failed: %s", e)
                out.append(0.0)
            self.n_rollouts += 1
        arr = np.asarray(out, float)
        self._cache[key] = arr
        return arr

    def cost(self, schedule_actions: dict[date, Action]) -> float:
        e = self.econ
        total = 0.0
        for a in schedule_actions.values():
            if a.kind == "none":
                continue
            total += e.application_fixed_cost_per_ha
            if a.kind == "fertilise":
                total += a.amount * e.n_cost_per_kg
            elif a.kind == "irrigate":
                total += a.amount * e.irrigation_cost_per_cm_ha
        return total

    def score(self, actions: dict[date, Action], alpha: float = 0.8) -> Schedule:
        events = Schedule(actions).as_events()
        y = self.yields(events)
        cost = self.cost(actions)
        margins = y * self.econ.crop_price_per_kg - cost
        return Schedule(
            actions=dict(actions),
            expected_value=float(margins.mean()),
            cvar=cvar(margins, alpha),
            mean_yield=float(y.mean()),
            yield_p05=float(np.quantile(y, 0.05)),
            cost=cost,
            n_applications=sum(1 for a in actions.values() if a.kind != "none"),
        )


def default_actions(max_n: float = 120.0) -> list[Action]:
    return [
        NO_ACTION,
        Action("fertilise", 30.0, "urea"),
        Action("fertilise", 60.0, "urea"),
        Action("fertilise", max_n, "urea"),
    ]


def decision_epochs(start: date, end: date, every_days: int = 14) -> list[date]:
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(days=every_days)
    return out


def beam_search(evaluator: ScenarioEvaluator, epochs: list[date],
                actions: list[Action] | None = None, beam_width: int = 6,
                alpha: float = 0.8, objective: str = "cvar",
                max_applications: int | None = 3) -> list[Schedule]:
    """Beam search over intervention schedules.

    Returns the beam, best first, so the UI can offer alternatives rather than a
    single opaque recommendation -- which is what makes the output usable by an
    agronomist who has context the model does not.
    """
    acts = actions or default_actions()
    beam: list[dict[date, Action]] = [{}]
    scored_beam: list[Schedule] = []

    for ep in epochs:
        cands: list[dict[date, Action]] = []
        for partial in beam:
            for a in acts:
                if a.kind != "none" and max_applications is not None:
                    used = sum(1 for x in partial.values() if x.kind != "none")
                    if used >= max_applications:
                        continue
                nxt = dict(partial)
                nxt[ep] = a
                cands.append(nxt)
        scored = [evaluator.score(c, alpha) for c in cands]
        key = (lambda s: s.cvar) if objective == "cvar" else (lambda s: s.expected_value)
        scored.sort(key=key, reverse=True)
        # Deduplicate on the *effective* schedule: two partials that differ only
        # in where a "none" sits are the same plan.
        seen, pruned = set(), []
        for s in scored:
            sig = tuple(sorted((str(d), str(a)) for d, a in s.actions.items()
                               if a.kind != "none"))
            if sig in seen:
                continue
            seen.add(sig)
            pruned.append(s)
            if len(pruned) >= beam_width:
                break
        scored_beam = pruned
        beam = [s.actions for s in pruned]

    for s in scored_beam:
        applied = [f"{d:%b %d}: {a.amount:g} kg N" for d, a in sorted(s.actions.items())
                   if a.kind == "fertilise"]
        s.rationale = ("no intervention" if not applied else "; ".join(applied)) + \
                      f" | CVaR{int(alpha*100)} {s.cvar:.0f}, mean {s.expected_value:.0f}"
    return scored_beam


def greedy(evaluator: ScenarioEvaluator, epochs: list[date],
           actions: list[Action] | None = None, alpha: float = 0.8,
           max_applications: int | None = 3) -> Schedule:
    """Beam search with width 1 -- the cheapest sensible baseline."""
    return beam_search(evaluator, epochs, actions, beam_width=1, alpha=alpha,
                       max_applications=max_applications)[0]


def mppi_schedule(evaluator: ScenarioEvaluator, epochs: list[date],
                  n_samples: int = 40, n_iters: int = 4, temperature: float = 1.0,
                  sigma: float = 30.0, max_n: float = 120.0, alpha: float = 0.8,
                  seed: int = 0) -> Schedule:
    """MPPI-style sampling MPC over a continuous relaxation of N rate.

    Implemented so the comparison against beam search is empirical rather than
    rhetorical. The relaxation is the standard one: treat the per-epoch N rate
    as continuous in [0, max_n], perturb the nominal sequence with Gaussian
    noise, weight rollouts by exp(-cost/temperature), take the weighted mean,
    and round to the discrete grid at the end.

    The rounding step is exactly where the method loses its guarantees, and the
    benchmark shows what that costs.
    """
    rng = np.random.default_rng(seed)
    nominal = np.zeros(len(epochs))
    grid = np.array([0.0, 30.0, 60.0, max_n])

    def to_actions(vec: np.ndarray) -> dict[date, Action]:
        out: dict[date, Action] = {}
        for ep, v in zip(epochs, vec):
            snapped = float(grid[np.argmin(np.abs(grid - v))])
            out[ep] = Action("fertilise", snapped, "urea") if snapped > 0 else NO_ACTION
        return out

    for _ in range(n_iters):
        noise = rng.normal(0.0, sigma, size=(n_samples, len(epochs)))
        samples = np.clip(nominal + noise, 0.0, max_n)
        scores = np.array([evaluator.score(to_actions(s), alpha).cvar for s in samples])
        # Softmax weighting on the (negated) cost, per MPPI.
        adv = scores - scores.max()
        w = np.exp(adv / max(temperature * (scores.std() + 1e-9), 1e-9))
        w /= w.sum()
        nominal = np.clip((w[:, None] * samples).sum(axis=0), 0.0, max_n)

    return evaluator.score(to_actions(nominal), alpha)
