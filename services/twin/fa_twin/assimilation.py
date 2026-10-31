"""Ensemble Kalman filter assimilation of UAV observations into WOFOST.

Why an EnKF, and why hand-rolled
--------------------------------
WOFOST is strongly nonlinear but its state dimension is small and a season runs
in ~0.15 s, so a 50-100 member ensemble costs seconds. That is the regime the
EnKF was designed for. A particle filter needs far more members for the same
skill and buys nothing here; a variational method needs an adjoint the model
does not have.

The libraries are not worth a dependency: filterpy's EnsembleKalmanFilter
assumes a callable state-transition matrix, which a crop model is not, and it
has had no release since 2018; DAPPER is a benchmarking framework for DA
research rather than a library to embed. The update below is ~60 lines of
NumPy, and every published WOFOST assimilation study effectively writes this.

What is actually being corrected
--------------------------------
Two knobs move crop-model error, and they are not interchangeable:

* **State update (EnKF on LAI).** Corrects where the crop *is* now. With sparse
  observations the model's own dynamics pull the state back within days, so a
  state update alone decays.
* **Parameter update (TSUM1/TSUM2/SPAN).** Corrects how the crop *develops*.
  This persists for the rest of the season, which is why the literature keeps
  finding that recalibrating phenology beats state assimilation when you have
  only 2-4 clear-sky observations per season.

We do both: the ensemble carries perturbed parameters, and the EnKF updates the
augmented state, so an observation informs both the current LAI and the
parameters that generated it. That is the "joint state-parameter" formulation,
and it is the reason the ensemble is built by perturbing parameters rather than
by adding noise to a single trajectory.

Two bugs fixed on 31 Oct 2026 (found reviewing the post-audit code)
------------------------------------------------------------------
1. **The parameter update never reached the crop.** The analysis wrote the
   updated TSUM1/TSUM2/SPAN/RGRLAI factors into a bookkeeping dict and nothing
   else, so the "joint" filter was really a state-only filter whose LAI nudge
   decays within days. That is consistent with the audit finding that one pass
   helped the 30-day canopy forecast but not yield. Updated parameters are now
   written into each member's running PCSE phenology and leaf-dynamics objects.
2. **Inflation was undone in the same line that applied it** (``A *= lam`` then
   ``xm + A/lam + K@innov``), so the posterior spread was never widened -- which
   fits the audit's 80 % intervals covering the truth only 60-75 % of the time.
   Inflation is now applied to the forecast ensemble before the analysis.

Parameters live in the augmented state as **log factors**, so every member
stays positive and a symmetric Gaussian update is a sensible approximation.
Observation operators are explicit: ``LAI`` (retrieved), ``CC`` (canopy cover,
``1 - exp(-k*LAI)``, what a phone camera actually measures) and ``DVS``
(growth stage, from BBCH staging). Several can be assimilated jointly.
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class ParameterPerturbation:
    """How much each uncertain parameter varies across the ensemble.

    Defaults are deliberately wide. They encode "we have not calibrated this
    cultivar for this site", which is the true state of knowledge for every
    Indian crop in the shipped parameter set. Narrow them only when a local
    calibration justifies it -- a tight prior you have not earned produces an
    ensemble that is confidently wrong and an EnKF that ignores the data.
    """
    TSUM1: float = 0.15      # relative sd on thermal time to anthesis
    TSUM2: float = 0.15      # ... to maturity
    SPAN: float = 0.10       # leaf lifespan
    TDWI: float = 0.20       # initial dry weight
    RGRLAI: float = 0.15     # max relative LAI growth rate
    WAV: float = 0.25        # initial available soil water
    NAVAILI: float = 0.25    # initial available N

    def sample(self, rng: np.random.Generator, n: int) -> dict[str, np.ndarray]:
        out = {}
        for name in self.__dataclass_fields__:
            sd = float(getattr(self, name))
            if sd <= 0:
                continue
            # Lognormal keeps every member strictly positive, which matters:
            # a negative TSUM or WAV is not a plausible draw, it is a crash.
            out[name] = np.exp(rng.normal(-0.5 * sd**2, sd, size=n))
        return out


@dataclass
class Observation:
    """One assimilable observation with its measurement error.

    ``sd`` is the standard deviation of the *observation operator*, not of the
    sensor. Retrieving LAI from a vegetation index carries far more error than
    the radiometry does, and pretending otherwise makes the filter overconfident
    and the analysis worse than the forecast.
    """
    day: date
    variable: str
    value: float
    sd: float


@dataclass
class AssimilationResult:
    days: list[date]
    prior_mean: list[float]
    posterior_mean: list[float]
    prior_sd: list[float]
    posterior_sd: list[float]
    observations: list[Observation]
    final_states: list[dict]
    parameter_posterior: dict[str, tuple[float, float]] = field(default_factory=dict)
    innovation: list[float] = field(default_factory=list)
    rejected: list[bool] = field(default_factory=list)


#: Beer-Lambert extinction for canopy cover from LAI (green cover, nadir view).
K_COVER = 0.6


def obs_operator(variable: str, state: dict) -> float:
    """Predicted observation for one member state.

    ``CC`` is green canopy cover, the quantity a fixed-exposure RGB photo gives
    directly (``phone.py``). Assimilating cover through its operator beat both
    a 25 %-error LAI retrieval and inverting cover to LAI in the audit (S2).
    """
    v = variable.upper()
    lai = float(state.get("LAI") or 0.0)
    if v == "LAI":
        return lai
    if v in ("CC", "COVER", "CANOPY_COVER"):
        return 1.0 - math.exp(-K_COVER * lai)
    if v == "DVS":
        return float(state.get("DVS") or 0.0)
    raise NotImplementedError(
        f"observation operator for {variable!r} is not implemented "
        "(supported: LAI, CC (canopy cover), DVS (growth stage))")


def _enkf_update(X: np.ndarray, HX: np.ndarray, y: np.ndarray, R: np.ndarray,
                 rng: np.random.Generator, loc: np.ndarray | None = None) -> np.ndarray:
    """One stochastic EnKF analysis on an (already inflated) ensemble.

    ``X`` is (n_state, n), ``HX`` the predicted observations (n_obs, n), ``y``
    and ``R`` the observations and their error variances. Using the ensemble's
    own predicted observations makes nonlinear operators (canopy cover) work
    without a Jacobian. ``loc`` damps the observation's reach into state rows
    it barely constrains; with ~30 members a sampling correlation of 0.2
    between LAI and, say, leaf lifespan is mostly noise.
    """
    n = X.shape[1]
    A = X - X.mean(axis=1, keepdims=True)
    HA = HX - HX.mean(axis=1, keepdims=True)
    PH = (A @ HA.T) / (n - 1)                          # (n_state, n_obs)
    HPH = (HA @ HA.T) / (n - 1)                        # (n_obs, n_obs)
    if loc is not None:
        PH = PH * loc.reshape(-1, 1)
    K = PH @ np.linalg.inv(HPH + np.diag(R))
    # Perturbed observations keep the posterior spread statistically correct.
    D = y.reshape(-1, 1) + rng.normal(0.0, 1.0, size=(len(y), n)) * np.sqrt(R).reshape(-1, 1)
    return X + K @ (D - HX)


class EnsembleTwin:
    """An ensemble of WOFOST runs with joint state-parameter assimilation.

    ``weathers`` (optional) gives each member its own weather -- a forecast
    ensemble. Without it every member shares ``weather``, which makes the
    ensemble a parameter-uncertainty ensemble only and its yield intervals too
    narrow for anything after the forecast horizon.
    """

    #: Parameters updated by the filter, in augmented-state order (as log factors).
    AUG_PARAMS = ("TSUM1", "TSUM2", "SPAN", "RGRLAI")
    #: Where each parameter lives inside a running PCSE crop.
    _PARAM_HOME = {"TSUM1": "pheno", "TSUM2": "pheno", "SPAN": "lv_dynamics",
                   "RGRLAI": "lv_dynamics"}
    #: State rows before the parameters.
    STATE_ROWS = ("LAI", "DVS", "TAGP", "TWSO")
    #: Parameter log factors are clipped to +-1 (x0.37 .. x2.7): beyond that a
    #: member is no longer the crop it claims to be.
    LOG_CLIP = 1.0

    def __init__(self, cfg, weather, n_members: int = 50,
                 perturbation: ParameterPerturbation | None = None,
                 events: list[dict] | None = None, seed: int = 0,
                 engine: str = "auto", inflation: float = 1.0,
                 localisation: float = 0.5, reject_sigma: float = 5.0,
                 weathers: list | None = None):
        self.cfg = cfg
        self.weather = weather
        self.weathers = list(weathers) if weathers else None
        if self.weathers is not None and len(self.weathers) != int(n_members):
            raise ValueError("weathers must have one entry per member")
        self.events = list(events or [])
        self.n = int(n_members)
        self.rng = np.random.default_rng(seed)
        self.pert = perturbation or ParameterPerturbation()
        self.engine = engine
        self.inflation = float(inflation)
        self.localisation = localisation
        self.reject_sigma = reject_sigma

        self._factors = self.pert.sample(self.rng, self.n)
        self.members: list = [self._make_member(i) for i in range(self.n)]

    # -- construction -----------------------------------------------------
    def _make_member(self, i: int):
        from .engine import ZoneTwin
        import copy

        cfg = copy.deepcopy(self.cfg)
        for name in ("WAV", "NAVAILI"):
            if name in self._factors:
                setattr(cfg, name, float(getattr(cfg, name) * self._factors[name][i]))
        w = self.weathers[i] if self.weathers else self.weather
        tw = ZoneTwin(cfg, w, events=self.events, engine=self.engine)
        pp = tw.model.parameterprovider
        tw._base = {}
        tw._perturb = {}
        for p in self.AUG_PARAMS + ("TDWI",):
            try:
                base = pp[p]
            except Exception:
                continue
            if not isinstance(base, (int, float)):
                continue
            f = float(self._factors[p][i]) if p in self._factors else 1.0
            if p in self.AUG_PARAMS:
                tw._base[p] = float(base)
                tw._perturb[p] = f
            # Overrides set before the crop starts are read when it is created.
            pp.set_override(p, float(base) * f, check=False)
        return tw

    # -- parameter write-back ---------------------------------------------
    def _write_params(self, m) -> None:
        """Push a member's current parameter factors into its running crop."""
        crop = getattr(m.model, "crop", None)
        if crop is None:          # before sowing: the override path still applies
            pp = m.model.parameterprovider
            for p, f in m._perturb.items():
                pp.set_override(p, m._base[p] * f, check=False)
            return
        for p, f in m._perturb.items():
            obj = getattr(crop, self._PARAM_HOME[p], None)
            if obj is not None and hasattr(obj.params, p):
                setattr(obj.params, p, float(m._base[p] * f))

    def current_parameters(self, i: int) -> dict[str, float]:
        """The parameter values member ``i`` is actually running with."""
        m = self.members[i]
        crop = getattr(m.model, "crop", None)
        out = {}
        for p in m._perturb:
            obj = getattr(crop, self._PARAM_HOME[p], None) if crop is not None else None
            out[p] = float(getattr(obj.params, p)) if obj is not None else m._base[p] * m._perturb[p]
        return out

    # -- stepping ---------------------------------------------------------
    def _augmented(self) -> np.ndarray:
        """State matrix: [LAI, DVS, TAGP, TWSO, *log parameter factors] x members."""
        rows = []
        for m in self.members:
            st = m.state()
            row = [float(st.get(v) or 0.0) for v in self.STATE_ROWS]
            row += [math.log(max(m._perturb.get(p, 1.0), 1e-6)) for p in self.AUG_PARAMS]
            rows.append(row)
        return np.asarray(rows, float).T

    def _localisation_vector(self) -> np.ndarray:
        loc = np.ones(len(self.STATE_ROWS) + len(self.AUG_PARAMS))
        loc[len(self.STATE_ROWS):] = self.localisation
        return loc

    def run_till(self, day: date) -> None:
        for m in self.members:
            if not m.terminated:
                try:
                    m.run_till(day)
                except Exception as e:  # a diverged member must not kill the run
                    log.warning("member failed at %s: %s", day, e)

    def assimilate(self, obs: "Observation | list[Observation]") -> tuple[float, bool]:
        """Analysis step for one observation or several taken together.

        Returns (innovation of the first observation, rejected). Each
        observation is gross-error checked on its own; rejected ones are left
        out and the rest are assimilated.
        """
        obs_list = list(obs) if isinstance(obs, (list, tuple)) else [obs]
        states = [m.state() for m in self.members]
        keep, first_innov, any_rejected = [], None, False
        for o in obs_list:
            hx = np.array([obs_operator(o.variable, st) for st in states])
            innov = o.value - float(hx.mean())
            if first_innov is None:
                first_innov = innov
            spread = float(hx.std(ddof=1)) if self.n > 1 else 0.0
            total_sd = math.sqrt(spread ** 2 + o.sd ** 2)
            # An observation many sigma from the whole ensemble is far more
            # likely a cloud, a shadow, a mis-registered photo or a bad
            # retrieval than a real crop state. Reject and flag it.
            if total_sd > 0 and abs(innov) > self.reject_sigma * total_sd:
                log.warning("rejecting %s observation %.3f on %s: %.1f sigma from ensemble",
                            o.variable, o.value, o.day, abs(innov) / total_sd)
                any_rejected = True
                continue
            keep.append((o, hx))
        if not keep:
            return float(first_innov or 0.0), True

        X = self._augmented()
        HX = np.vstack([hx for _, hx in keep])
        if self.inflation != 1.0:
            xm = X.mean(axis=1, keepdims=True)
            hm = HX.mean(axis=1, keepdims=True)
            X = xm + self.inflation * (X - xm)
            HX = hm + self.inflation * (HX - hm)
        y = np.array([o.value for o, _ in keep])
        R = np.array([o.sd ** 2 for o, _ in keep])
        Xa = _enkf_update(X, HX, y, R, self.rng, loc=self._localisation_vector())

        ns = len(self.STATE_ROWS)
        lai_a = np.clip(Xa[0, :], 1e-3, 12.0)
        logf = np.clip(Xa[ns:, :], -self.LOG_CLIP, self.LOG_CLIP)
        for i, m in enumerate(self.members):
            if m.terminated:
                continue
            for j, p in enumerate(self.AUG_PARAMS):
                if p in m._perturb:
                    m._perturb[p] = float(math.exp(logf[j, i]))
            self._write_params(m)
            try:
                if (m.state().get("LAI") or 0.0) > 0:
                    m.set_lai(float(lai_a[i]))
            except Exception as e:
                log.warning("could not set LAI on member %d: %s", i, e)
        return float(first_innov), any_rejected

    def run(self, observations: list[Observation]) -> AssimilationResult:
        """Assimilate observations in date order (same-day ones jointly), then finish."""
        res = AssimilationResult([], [], [], [], [], list(observations), [])
        by_day: dict[date, list[Observation]] = {}
        for o in observations:
            by_day.setdefault(o.day, []).append(o)
        for day in sorted(by_day):
            self.run_till(day)
            X = self._augmented()
            res.days.append(day)
            res.prior_mean.append(float(X[0].mean()))
            res.prior_sd.append(float(X[0].std(ddof=1)) if self.n > 1 else 0.0)
            innov, rejected = self.assimilate(by_day[day])
            Xp = self._augmented()
            res.posterior_mean.append(float(Xp[0].mean()))
            res.posterior_sd.append(float(Xp[0].std(ddof=1)) if self.n > 1 else 0.0)
            res.innovation.append(innov)
            res.rejected.append(rejected)
        for m in self.members:
            if not m.terminated:
                try:
                    m.run_to_end()
                except Exception as e:
                    log.warning("member failed to finish: %s", e)
        res.final_states = [m.summary() for m in self.members]
        for p in self.AUG_PARAMS:
            vals = np.array([m._perturb.get(p, 1.0) for m in self.members])
            res.parameter_posterior[p] = (
                float(vals.mean()), float(vals.std(ddof=1)) if len(vals) > 1 else 0.0)
        return res

    # -- outputs ----------------------------------------------------------
    def yield_distribution(self) -> tuple[float, float, np.ndarray]:
        vals = np.array([float(s.get("TWSO") or 0.0) for s in
                         (m.summary() for m in self.members)])
        sd = float(vals.std(ddof=1)) if len(vals) > 1 else 0.0
        return float(vals.mean()), sd, vals

    def quantiles(self, qs=(0.05, 0.5, 0.95)) -> dict[float, float]:
        _, _, vals = self.yield_distribution()
        return {q: float(np.quantile(vals, q)) for q in qs}

    def interval(self, level: float = 0.8) -> tuple[float, float, float]:
        """Yield interval that holds its level for a finite ensemble.

        If the truth is exchangeable with the n members, it falls between the
        k-th and m-th smallest member with probability (m - k)/(n + 1). The
        usual ``np.percentile(vals, [10, 90])`` on 24 members therefore covers
        only ~74 %, not 80 % -- which is most of what the audit read as an
        "overconfident" ensemble (open loop covered 0.70 +- 0.06 in
        ``bench/one_pass_osse.py``). Choosing order statistics so that
        (m - k)/(n + 1) >= level fixes it without touching the filter.

        Returns (low, high, guaranteed_level); with too few members for the
        level the full range is returned and the guarantee is smaller.
        """
        _, _, vals = self.yield_distribution()
        v = np.sort(vals)
        n = len(v)
        a = (1.0 - level) / 2.0
        k = int(math.floor(a * (n + 1)))           # 1-based lower order statistic
        m = int(math.ceil((1.0 - a) * (n + 1)))    # 1-based upper order statistic
        k, m = max(1, k), min(n, m)
        return float(v[k - 1]), float(v[m - 1]), (m - k) / (n + 1)
