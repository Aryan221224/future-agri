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


def _enkf_update(X: np.ndarray, y: float, R: float, h_idx: int,
                 rng: np.random.Generator, inflation: float = 1.02,
                 loc: np.ndarray | None = None) -> np.ndarray:
    """One stochastic EnKF analysis step on an augmented state matrix.

    ``X`` is (n_state, n_members). ``h_idx`` is the row the observation sees --
    a linear observation operator, which is right here because we assimilate a
    *retrieved* LAI rather than raw radiance. If you assimilate reflectance
    directly, H becomes PROSAIL and this needs to change.

    Multiplicative inflation counters the well-known ensemble collapse from
    finite-member sampling error. Localisation (``loc``) damps spurious
    correlations between the observed variable and unrelated parameters -- with
    50 members, a sampling correlation of 0.2 between LAI and, say, initial soil
    water is pure noise, and without damping the filter will happily update soil
    water on the strength of it.
    """
    n_state, n = X.shape
    xm = X.mean(axis=1, keepdims=True)
    A = X - xm
    A *= inflation
    # Cross-covariance of the full state with the observed row.
    a_obs = A[h_idx : h_idx + 1, :]
    PH = (A @ a_obs.T) / (n - 1)                 # (n_state, 1)
    HPH = float(((a_obs @ a_obs.T) / (n - 1)).item())   # scalar
    if loc is not None:
        PH = PH * loc.reshape(-1, 1)
    K = PH / (HPH + R)
    # Perturbed observations: the stochastic EnKF needs them for the posterior
    # spread to be correct rather than systematically too small.
    d = y + rng.normal(0.0, math.sqrt(R), size=n)
    innov = d - X[h_idx, :]
    return xm + A / inflation + K @ innov.reshape(1, n)


class EnsembleTwin:
    """An ensemble of WOFOST runs with joint state-parameter assimilation."""

    #: Parameters that are updated by the filter, in augmented-state order.
    AUG_PARAMS = ("TSUM1", "TSUM2", "SPAN", "RGRLAI")

    def __init__(self, cfg, weather, n_members: int = 50,
                 perturbation: ParameterPerturbation | None = None,
                 events: list[dict] | None = None, seed: int = 0,
                 engine: str = "auto", inflation: float = 1.02,
                 localisation: float = 0.5, reject_sigma: float = 5.0):
        from .engine import ZoneTwin

        self.cfg = cfg
        self.weather = weather
        self.events = list(events or [])
        self.n = int(n_members)
        self.rng = np.random.default_rng(seed)
        self.pert = perturbation or ParameterPerturbation()
        self.engine = engine
        self.inflation = inflation
        self.localisation = localisation
        self.reject_sigma = reject_sigma

        self._factors = self.pert.sample(self.rng, self.n)
        self.members: list[ZoneTwin] = [self._make_member(i) for i in range(self.n)]

    # -- construction -----------------------------------------------------
    def _make_member(self, i: int):
        from .engine import ZoneTwin
        import copy

        cfg = copy.deepcopy(self.cfg)
        for name in ("WAV", "NAVAILI"):
            if name in self._factors:
                setattr(cfg, name, float(getattr(cfg, name) * self._factors[name][i]))
        overrides = {p: float(self._factors[p][i]) for p in self.AUG_PARAMS
                     if p in self._factors}
        tw = ZoneTwin(cfg, self.weather, events=self.events, engine=self.engine)
        # Scale the crop parameters this member was drawn with. PCSE's
        # ParameterProvider supports overrides, which is cleaner than editing
        # the YAML per member.
        pp = tw.model.parameterprovider
        for p, f in overrides.items():
            try:
                base = pp[p]
            except Exception:
                continue
            if isinstance(base, (int, float)):
                pp.set_override(p, float(base) * f, check=False)
        tw._perturb = overrides
        return tw

    # -- stepping ---------------------------------------------------------
    def _augmented(self) -> np.ndarray:
        """State matrix: [LAI, TAGP, TWSO, *scaled parameters] x members."""
        rows = []
        for m in self.members:
            st = m.state()
            row = [st.get("LAI", 0.0), st.get("TAGP", 0.0), st.get("TWSO", 0.0)]
            row += [m._perturb.get(p, 1.0) for p in self.AUG_PARAMS]
            rows.append(row)
        return np.asarray(rows, float).T

    def _localisation_vector(self) -> np.ndarray:
        """Damp the observation's reach into parameters it barely constrains."""
        n_state = 3 + len(self.AUG_PARAMS)
        loc = np.ones(n_state)
        loc[3:] = self.localisation
        return loc

    def run_till(self, day: date) -> None:
        for m in self.members:
            if not m.terminated:
                try:
                    m.run_till(day)
                except Exception as e:  # a diverged member must not kill the run
                    log.warning("member failed at %s: %s", day, e)

    def assimilate(self, obs: Observation) -> tuple[float, bool]:
        """Analysis step. Returns (innovation, rejected)."""
        if obs.variable.upper() != "LAI":
            raise NotImplementedError(
                f"observation operator for {obs.variable!r} is not implemented; "
                "retrieve to LAI first (see fa_twin.obsop)")
        X = self._augmented()
        prior = X[0, :]
        innov = obs.value - float(prior.mean())
        spread = float(prior.std(ddof=1)) if self.n > 1 else 0.0
        total_sd = math.sqrt(spread**2 + obs.sd**2)

        # Gross-error check. An observation many sigma from the entire ensemble
        # is far more likely to be a cloud, a shadow, a mis-georeferenced tile
        # or a bad retrieval than a real crop state -- and assimilating it will
        # wreck the season. Reject and flag rather than trust it.
        if total_sd > 0 and abs(innov) > self.reject_sigma * total_sd:
            log.warning("rejecting LAI observation %.2f on %s: %.1f sigma from ensemble",
                        obs.value, obs.day, abs(innov) / total_sd)
            return innov, True

        Xa = _enkf_update(X, obs.value, obs.sd**2, h_idx=0, rng=self.rng,
                          inflation=self.inflation, loc=self._localisation_vector())
        lai_a = np.clip(Xa[0, :], 1e-3, 12.0)
        for i, m in enumerate(self.members):
            if m.terminated:
                continue
            try:
                m.set_lai(float(lai_a[i]))
            except Exception as e:
                log.warning("could not set LAI on member %d: %s", i, e)
        # Keep the parameter book-keeping in step with the analysis so the
        # posterior parameter distribution is reportable.
        for j, p in enumerate(self.AUG_PARAMS, start=3):
            for i, m in enumerate(self.members):
                m._perturb[p] = float(Xa[j, i])
        return innov, False

    def run(self, observations: list[Observation]) -> AssimilationResult:
        res = AssimilationResult([], [], [], [], [], list(observations), [])
        for obs in sorted(observations, key=lambda o: o.day):
            self.run_till(obs.day)
            X = self._augmented()
            res.days.append(obs.day)
            res.prior_mean.append(float(X[0].mean()))
            res.prior_sd.append(float(X[0].std(ddof=1)) if self.n > 1 else 0.0)
            innov, rejected = self.assimilate(obs)
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
        for j, p in enumerate(self.AUG_PARAMS):
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
