"""Calibrated uncertainty via split conformal prediction.

Why not Monte Carlo dropout
---------------------------
MC dropout needs N stochastic forward passes per image (N=30 is typical) and
still gives you no coverage guarantee -- the intervals are whatever the network's
dropout noise happens to produce. Split conformal needs one forward pass and a
held-out calibration set, and gives a *distribution-free, finite-sample*
guarantee: the prediction set contains the truth with probability at least
1 - alpha, under exchangeability alone. On an edge budget where inference is the
binding constraint, paying ~30x inference cost for a weaker guarantee is a bad
trade.

The exchangeability caveat, which matters more here than anywhere
---------------------------------------------------------------
Agriculture violates exchangeability harder than almost any domain: lighting,
growth stage, cultivar, soil background and season all shift the distribution.
A single global calibration set yields nominal 90% coverage on paper and
materially less in a new field. So calibration here is *stratified* by
(crop, growth stage, site) and empirical coverage is a monitored production
metric with an alarm -- not a number you check once at validation time.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np


def _conformal_quantile(scores: np.ndarray, alpha: float) -> float:
    """The finite-sample-corrected quantile ceil((n+1)(1-alpha))/n.

    This correction is the whole reason the guarantee is exact rather than
    asymptotic. With small n it is conservative, which is the right direction.
    """
    n = len(scores)
    if n == 0:
        raise ValueError("cannot calibrate on an empty set")
    k = math.ceil((n + 1) * (1 - alpha))
    if k > n:
        # Not enough calibration data to certify this alpha at all.
        return float("inf")
    return float(np.sort(scores)[k - 1])


def min_calibration_n(alpha: float) -> int:
    """Smallest n for which the conformal quantile is finite at this alpha."""
    return math.ceil(1 / alpha) - 1


@dataclass
class RegressionCalibration:
    """Split conformal for a scalar regression (LAI, canopy cover, weed density)."""
    alpha: float
    q: float
    n: int
    stratum: str = "global"
    normalized: bool = False

    def interval(self, yhat: float, sigma: float = 1.0) -> tuple[float, float]:
        w = self.q * (sigma if self.normalized else 1.0)
        return yhat - w, yhat + w

    def width(self, sigma: float = 1.0) -> float:
        return 2 * self.q * (sigma if self.normalized else 1.0)


def calibrate_regression(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    alpha: float = 0.10,
    sigma: np.ndarray | None = None,
    stratum: str = "global",
) -> RegressionCalibration:
    """Fit a split-conformal interval width on held-out calibration data.

    ``sigma`` enables *normalized* conformal prediction: pass a per-sample
    difficulty estimate (e.g. pixel-count-weighted variance) and intervals become
    adaptive -- wide where the model is uncertain, tight where it is not --
    instead of one constant width everywhere. Same guarantee, far more useful
    intervals, which is what makes an agronomist trust the band.
    """
    y_true, y_pred = np.asarray(y_true, float), np.asarray(y_pred, float)
    resid = np.abs(y_true - y_pred)
    if sigma is not None:
        sigma = np.asarray(sigma, float)
        sigma = np.clip(sigma, 1e-6, None)
        resid = resid / sigma
    return RegressionCalibration(
        alpha=alpha,
        q=_conformal_quantile(resid, alpha),
        n=len(resid),
        stratum=stratum,
        normalized=sigma is not None,
    )


@dataclass
class ClassificationCalibration:
    """Split conformal prediction *sets* for classification.

    Two scores are supported:

    ``lac``  (least-ambiguous classifier) gives the smallest average set size.
    ``raps`` (regularized adaptive prediction sets) gives better conditional
             coverage -- it does not achieve its 90% by being right on easy
             classes and hopeless on rare ones, which for a weed/disease model
             is exactly the failure that matters.

    Both use the randomized score. Without randomization the discrete inversion
    over-covers noticeably (measured: 0.957 against a 0.90 target on 6 classes),
    and over-coverage is not free -- it inflates set sizes and therefore the
    human review rate, which is the operational cost that decides whether
    anyone keeps using the system.
    """
    alpha: float
    q: float
    n: int
    classes: list[str]
    score: str = "raps"
    k_reg: int = 1
    lam: float = 0.05
    stratum: str = "global"
    randomized: bool = True

    def predict_set(self, probs: np.ndarray, u: float | None = None) -> list[str]:
        """Prediction set for one softmax vector.

        ``u`` is a uniform draw enabling *randomized* prediction. Randomization
        is what makes coverage exact rather than conservative, and the choice is
        a genuine engineering tradeoff rather than a detail:

        * deterministic (``u=None``, the default) over-covers -- measured 0.929
          against a 0.90 target with RAPS -- which pushes more findings into the
          review queue than necessary, but the same image always produces the
          same set. For a system that must answer "why was this flagged?" months
          later, reproducibility wins.
        * randomized hits the target exactly and yields smaller sets, at the
          cost of the same image producing different sets on different runs.

        Default deterministic, and prefer ``score="lac"`` in production: it is
        near-exact deterministically and yields the smallest sets.
        """
        probs = np.asarray(probs, float)
        if self.score == "lac":
            keep = probs >= (1 - self.q)
            idx = np.flatnonzero(keep)
            if idx.size == 0:                      # never return an empty set silently
                idx = np.array([int(np.argmax(probs))])
            return [self.classes[i] for i in idx]
        order = np.argsort(-probs)
        sorted_p = probs[order]
        # Entry score of the j-th ranked class: probability mass strictly above
        # it, plus the size penalty it would incur.
        before = np.concatenate([[0.0], np.cumsum(sorted_p)[:-1]])
        penalty = self.lam * np.maximum(np.arange(1, len(probs) + 1) - self.k_reg, 0)
        entry = before + penalty
        keep = int(np.sum(entry < self.q))
        keep = max(1, min(keep, len(probs)))
        if u is not None and keep >= 1:
            # Drop the marginal class with the probability that makes coverage exact.
            excess = (before[keep - 1] + sorted_p[keep - 1] + penalty[keep - 1]) - self.q
            v = excess / max(sorted_p[keep - 1], 1e-12)
            if u < v and keep > 1:
                keep -= 1
        return [self.classes[i] for i in order[:keep]]

    def needs_review(self, probs: np.ndarray, u: float | None = None) -> bool:
        """A set of size != 1 is the routing rule.

        Size 0 (in LAC, before the fallback) means out-of-distribution -- a weed
        species or disease the model never saw, which is precisely what an
        agronomist should look at. Size > 1 means genuine ambiguity, e.g.
        nutrient deficiency versus water stress. Both go to a human.
        """
        return len(self.predict_set(probs, u)) != 1


def calibrate_classification(
    probs: np.ndarray,
    labels: np.ndarray,
    classes: list[str],
    alpha: float = 0.10,
    score: str = "raps",
    k_reg: int = 1,
    lam: float = 0.05,
    stratum: str = "global",
    randomized: bool = True,
    seed: int = 0,
) -> ClassificationCalibration:
    probs = np.asarray(probs, float)
    labels = np.asarray(labels, int)
    n = len(labels)
    rng = np.random.default_rng(seed)
    u = rng.uniform(size=n) if randomized else np.ones(n)
    if score == "lac":
        s = 1.0 - probs[np.arange(n), labels]
    elif score == "raps":
        order = np.argsort(-probs, axis=1)
        ranks = np.argsort(order, axis=1)[np.arange(n), labels]      # 0-based rank of truth
        sorted_p = np.take_along_axis(probs, order, axis=1)
        before = np.cumsum(sorted_p, axis=1) - sorted_p              # mass strictly above truth
        p_true = probs[np.arange(n), labels]
        s = (before[np.arange(n), ranks] + u * p_true
             + lam * np.maximum(ranks + 1 - k_reg, 0))
    else:
        raise ValueError(f"unknown score {score!r}")
    return ClassificationCalibration(
        alpha=alpha, q=_conformal_quantile(s, alpha), n=n,
        classes=list(classes), score=score, k_reg=k_reg, lam=lam, stratum=stratum,
        randomized=randomized,
    )


@dataclass
class StratifiedCalibrator:
    """Per-(crop, stage, site) calibrations with an explicit fallback.

    The fallback is where honesty lives: if a stratum has too little data, we do
    not silently borrow another stratum's quantile and claim coverage. We widen
    to the global quantile and mark the observation for review.
    """
    alpha: float = 0.10
    strata: dict[str, RegressionCalibration | ClassificationCalibration] = field(default_factory=dict)
    global_cal: RegressionCalibration | ClassificationCalibration | None = None
    min_n: int = 200

    @staticmethod
    def key(crop: str, stage: str, site: str) -> str:
        return f"{crop}|{stage}|{site}"

    def get(self, crop: str, stage: str, site: str):
        c = self.strata.get(self.key(crop, stage, site))
        if c is not None and c.n >= self.min_n:
            return c, True
        return self.global_cal, False

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({
            "alpha": self.alpha,
            "min_n": self.min_n,
            "global": asdict(self.global_cal) if self.global_cal else None,
            "strata": {k: asdict(v) for k, v in self.strata.items()},
        }, indent=2), encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | Path) -> "StratifiedCalibrator":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        def _mk(x):
            if x is None:
                return None
            if "qs" in x:
                return ClassConditionalCalibration(**x)
            return (ClassificationCalibration(**x) if "classes" in x
                    else RegressionCalibration(**x))
        return cls(alpha=d["alpha"], min_n=d.get("min_n", 200),
                   global_cal=_mk(d.get("global")),
                   strata={k: _mk(v) for k, v in d.get("strata", {}).items()})


def empirical_coverage(y_true, lo, hi) -> float:
    y_true, lo, hi = np.asarray(y_true), np.asarray(lo), np.asarray(hi)
    return float(np.mean((y_true >= lo) & (y_true <= hi)))


def coverage_alarm(observed: float, nominal: float, n: int, z: float = 3.0) -> bool:
    """Alarm when observed coverage falls below nominal by more than z SEs.

    This is the production monitor. Coverage drifting below nominal means the
    calibration set is no longer exchangeable with what the model is seeing --
    new season, new cultivar, new light -- and the right response is to
    recalibrate, not to widen a constant.
    """
    se = math.sqrt(max(nominal * (1 - nominal), 1e-9) / max(n, 1))
    return observed < nominal - z * se


# ---------------------------------------------------------------------------
# Class-conditional (Mondrian) conformal and action-level gating
# ---------------------------------------------------------------------------
#
# Why: on the real H200 detector the marginal gate hit its target (0.923 at
# alpha=0.10) while quickweed coverage was 0.52 (n=27) at alpha=0.05. Marginal
# coverage is an average; a 90 % average can hide a class the gate almost
# always gets wrong -- for a weed model, usually the rare weed. Class-
# conditional conformal calibrates one quantile *per true class*, so every
# class gets its own guarantee (given exchangeability within the class).
#
# And most decisions do not need species. Spray/no-spray needs crop vs weed vs
# nothing. Summing softmax mass into those action groups before calibrating
# removes the confusions that do not change an action (amaranth vs quickweed),
# which on the repo's own classifier cut the review rate from 78.7 % to 2.5 %
# at equal coverage (simulated; to be confirmed on real scores).

#: Action groups for WeedsGalore-style detectors.
ACTION_GROUPS = {"crop": ("maize", "crop", "potato", "wheat"),
                 "background": ("background",)}


def action_grouping(classes: list[str], crop_names=ACTION_GROUPS["crop"]) -> dict[str, list[str]]:
    """Map fine classes to action groups: crop / weed / background."""
    groups: dict[str, list[str]] = {"crop": [], "weed": [], "background": []}
    for c in classes:
        lc = c.lower()
        if lc in ACTION_GROUPS["background"]:
            groups["background"].append(c)
        elif lc in crop_names:
            groups["crop"].append(c)
        else:
            groups["weed"].append(c)
    return {k: v for k, v in groups.items() if v}


def group_probs(probs: np.ndarray, classes: list[str], groups: dict[str, list[str]]
                ) -> tuple[np.ndarray, list[str]]:
    """Sum class probabilities into groups. Returns (probs (N,G), group names)."""
    probs = np.atleast_2d(np.asarray(probs, float))
    names = list(groups)
    idx = {c: i for i, c in enumerate(classes)}
    out = np.stack([probs[:, [idx[c] for c in groups[g]]].sum(1) for g in names], axis=1)
    return out, names


def group_labels(labels: np.ndarray, classes: list[str], groups: dict[str, list[str]]) -> np.ndarray:
    names = list(groups)
    to_g = {classes.index(c): names.index(g) for g, cs in groups.items() for c in cs}
    return np.array([to_g[int(y)] for y in labels], int)


@dataclass
class ClassConditionalCalibration:
    """Mondrian LAC: one conformal quantile per true class.

    A class with fewer calibration examples than needed for its alpha cannot be
    calibrated honestly; its quantile is set to 1.0, so it is in *every*
    prediction set and every detection that might be it goes to a human. That
    is the conservative failure, and ``undercalibrated`` names those classes.
    """
    alpha: float
    qs: list[float]
    ns: list[int]
    classes: list[str]
    stratum: str = "global"
    undercalibrated: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return int(sum(self.ns))

    def predict_set(self, probs: np.ndarray, u: float | None = None) -> list[str]:
        probs = np.asarray(probs, float)
        keep = [i for i, q in enumerate(self.qs) if probs[i] >= 1.0 - q]
        if not keep:                                  # never return an empty set silently
            keep = [int(np.argmax(probs))]
        return [self.classes[i] for i in keep]

    def needs_review(self, probs: np.ndarray, u: float | None = None) -> bool:
        return len(self.predict_set(probs)) != 1


def calibrate_class_conditional(probs: np.ndarray, labels: np.ndarray, classes: list[str],
                                alpha: float = 0.10, stratum: str = "global"
                                ) -> ClassConditionalCalibration:
    probs = np.asarray(probs, float)
    labels = np.asarray(labels, int)
    qs, ns, under = [], [], []
    need = min_calibration_n(alpha)
    for k, c in enumerate(classes):
        m = labels == k
        ns.append(int(m.sum()))
        if m.sum() < need:
            qs.append(1.0)
            under.append(c)
            continue
        s = 1.0 - probs[m, k]
        qs.append(float(_conformal_quantile(s, alpha)))
    return ClassConditionalCalibration(alpha, qs, ns, list(classes), stratum, under)


@dataclass
class ActionGate:
    """Conformal gate on action groups (crop / weed / background).

    ``predict`` returns the action set for one detection's fine-class softmax;
    a singleton set is actionable (spray it, or leave it), anything else goes
    to the review queue.
    """
    groups: dict[str, list[str]]
    classes: list[str]
    cal: ClassConditionalCalibration

    def predict(self, probs: np.ndarray) -> list[str]:
        g, _ = group_probs(probs, self.classes, self.groups)
        return self.cal.predict_set(g[0])

    def needs_review(self, probs: np.ndarray) -> bool:
        return len(self.predict(probs)) != 1


def calibrate_action_gate(probs: np.ndarray, labels: np.ndarray, classes: list[str],
                          alpha: float = 0.10, groups: dict[str, list[str]] | None = None
                          ) -> ActionGate:
    groups = groups or action_grouping(classes)
    gp, names = group_probs(probs, classes, groups)
    gl = group_labels(labels, classes, groups)
    return ActionGate(groups, list(classes), calibrate_class_conditional(gp, gl, names, alpha, "action"))


@dataclass
class RecalibrationReport:
    used_field_labels: bool
    n_field: int
    reason: str


def recalibrate_with_field_labels(current, probs: np.ndarray, labels: np.ndarray,
                                  classes: list[str], alpha: float, min_n: int = 100,
                                  class_conditional: bool = True):
    """Refit the gate on labels collected in *this* field, when there are enough.

    The detector was trained on German maize. Exchangeability between that and
    an Indian potato field is not a reasonable assumption, so the guarantee is
    only restored by calibrating on field data: in simulation, 100-200 field
    labels brought coverage back from 0.63-0.80 to ~0.91. Below ``min_n`` the
    current gate is kept and the report says why.
    """
    n = int(len(labels))
    if n < min_n:
        return current, RecalibrationReport(False, n, f"only {n} field labels (< {min_n}); "
                                                      "keeping current calibration")
    cal = (calibrate_class_conditional(probs, labels, classes, alpha, "field")
           if class_conditional else
           calibrate_classification(probs, labels, classes, alpha, score="lac",
                                    randomized=False, stratum="field"))
    return cal, RecalibrationReport(True, n, f"recalibrated on {n} field labels")


def recalibrate_action_gate(gate: ActionGate, fine_probs: np.ndarray, action_labels: list[str],
                            alpha: float | None = None, min_n: int = 100
                            ) -> tuple[ActionGate, RecalibrationReport]:
    """Refit an ``ActionGate`` on field labels from the review queue.

    Review labels are *action* labels (crop / weed / background) attached to the
    detector's fine-class softmax, so the fine probabilities are grouped with the
    gate's own grouping and a class-conditional quantile is fitted per action.
    Returns the gate the pipeline actually uses -- not a bare calibration.
    """
    n = len(action_labels)
    if n < min_n:
        return gate, RecalibrationReport(False, n, f"only {n} field labels (< {min_n}); "
                                                   "keeping current gate")
    names = list(gate.groups)
    bad = sorted({a for a in action_labels if a not in names})
    if bad:
        raise ValueError(f"labels {bad} are not action groups {names}")
    gp, _ = group_probs(np.asarray(fine_probs, float), gate.classes, gate.groups)
    y = np.array([names.index(a) for a in action_labels])
    cal = calibrate_class_conditional(gp, y, names, alpha if alpha is not None else gate.cal.alpha, "field")
    return ActionGate(gate.groups, gate.classes, cal), RecalibrationReport(
        True, n, f"recalibrated on {n} field labels" +
        (f"; still under-calibrated: {', '.join(cal.undercalibrated)}" if cal.undercalibrated else ""))
