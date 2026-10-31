"""Crop twin: feasibility gates, engine behaviour, and assimilation."""
import datetime as dt
import numpy as np
import pytest

from fa_twin.assimilation import EnsembleTwin, Observation, ParameterPerturbation
from fa_twin.engine import (ZoneConfig, ZoneTwin, check_photoperiod,
                            max_daylength_h, resolve_start_type, variety_support)
from fa_twin.weather import make_synthetic_weather

PUNJAB = dict(latitude=30.90, longitude=75.85)


def _rice_cfg(**kw):
    base = dict(zone_id="t", crop="rice", variety="Rice_501",
                sowing_date=dt.date(2025, 6, 20), harvest_date=dt.date(2025, 10, 25),
                WAV=35.0, NAVAILI=5.0, NSOILBASE=2.0, **PUNJAB)
    return ZoneConfig(**{**base, **kw})


@pytest.fixture(scope="module")
def weather():
    return make_synthetic_weather(30.90, 75.85, dt.date(2025, 6, 1), days=200, seed=7)


def test_daylength_is_latitude_dependent():
    assert max_daylength_h(30.90) < max_daylength_h(51.97)
    assert 14.0 < max_daylength_h(30.90) < 15.5


def test_european_wheat_is_rejected_at_indian_latitude():
    """The silent-zero-yield trap. This must never regress."""
    cfg = _rice_cfg(crop="wheat", variety="Winter_wheat_101",
                    sowing_date=dt.date(2024, 11, 10), harvest_date=dt.date(2025, 4, 20))
    f = check_photoperiod(cfg)
    assert not f.ok
    assert any("stall" in r or "cannot" in r for r in f.reasons)


def test_same_cultivar_is_feasible_at_its_design_latitude():
    cfg = _rice_cfg(crop="wheat", variety="Winter_wheat_101", latitude=51.97,
                    longitude=5.66, sowing_date=dt.date(2024, 11, 10),
                    harvest_date=dt.date(2025, 4, 20))
    assert check_photoperiod(cfg).ok


def test_variety_support_differs_between_engines():
    """wofost81 ships 9 rice varieties but only one is actually runnable."""
    v81 = variety_support("rice", "Wofost81_NWLP_CWB_CNB")
    v72 = variety_support("rice", "Wofost72_WLP_CWB")
    assert len(v81) < len(v72)
    assert "Rice_501" in v81


def test_transplanted_crop_starts_at_emergence():
    assert resolve_start_type(_rice_cfg()) == "emergence"
    assert resolve_start_type(_rice_cfg(crop="maize", variety="Grain_maize_201")) == "sowing"


def test_uncalibrated_crops_carry_a_warning(weather):
    cfg = _rice_cfg(crop="cotton", variety="Cotton_VanHeemst_1988",
                    sowing_date=dt.date(2025, 6, 15), harvest_date=dt.date(2025, 11, 20))
    tw = ZoneTwin(cfg, weather, engine="Wofost72_WLP_CWB")
    tw.run_to_end()
    s = tw.summary()
    assert s["uncalibrated_parameters"] is True
    assert "warning" in s


def test_synthetic_weather_is_flagged_not_for_advice(weather):
    tw = ZoneTwin(_rice_cfg(), weather, engine="Wofost72_WLP_CWB")
    tw.run_to_end()
    assert "NOT-FOR-ADVICE" in tw.summary()["weather_source"]


def test_season_completes_and_produces_grain(weather):
    tw = ZoneTwin(_rice_cfg(), weather, engine="Wofost72_WLP_CWB")
    tw.run_to_end()
    s = tw.summary()
    assert s["DVS"] >= 1.9          # reached maturity
    assert s["TWSO"] > 0


def test_nitrogen_has_a_monotone_non_decreasing_effect(weather):
    ys = []
    for n in (0.0, 30.0, 60.0):
        tw = ZoneTwin(_rice_cfg(), weather,
                      events=[{"date": dt.date(2025, 7, 10), "N_amount": n}],
                      engine="Wofost81_NWLP_CWB_CNB")
        tw.run_to_end()
        ys.append(float(tw.summary()["TWSO"]))
    assert ys[1] > ys[0]                 # fertiliser helps when N is limiting
    assert ys[2] >= ys[1] - 1e-6         # and never hurts


def test_setting_lai_propagates_into_leaf_biomass(weather):
    tw = ZoneTwin(_rice_cfg(), weather, engine="Wofost72_WLP_CWB")
    tw.run_till(dt.date(2025, 8, 15))
    before = tw.state()
    delta = tw.set_lai(before["LAI"] * 0.5)
    assert delta["LAI"] < 0
    assert delta["WLV"] < 0              # rescaled, not overwritten


def test_gross_outlier_observations_are_rejected(weather):
    ens = EnsembleTwin(_rice_cfg(), weather, n_members=8, seed=1,
                       engine="Wofost72_WLP_CWB")
    ens.run_till(dt.date(2025, 8, 15))
    _, rejected = ens.assimilate(Observation(dt.date(2025, 8, 15), "LAI", 99.0, 0.2))
    assert rejected


def test_assimilation_moves_the_ensemble_toward_the_observation(weather):
    ens = EnsembleTwin(_rice_cfg(), weather, n_members=16,
                       perturbation=ParameterPerturbation(TSUM1=0.2, TSUM2=0.2,
                                                          SPAN=0.2, RGRLAI=0.2,
                                                          TDWI=0.0, WAV=0.2, NAVAILI=0.0),
                       seed=2, engine="Wofost72_WLP_CWB")
    ens.run_till(dt.date(2025, 8, 20))
    prior = ens._augmented()[0].mean()
    target = prior * 1.6
    ens.assimilate(Observation(dt.date(2025, 8, 20), "LAI", float(target), 0.05))
    post = ens._augmented()[0].mean()
    assert abs(post - target) < abs(prior - target)


def test_unsupported_observation_variable_raises(weather):
    ens = EnsembleTwin(_rice_cfg(), weather, n_members=4, seed=3,
                       engine="Wofost72_WLP_CWB")
    ens.run_till(dt.date(2025, 8, 1))
    with pytest.raises(NotImplementedError):
        ens.assimilate(Observation(dt.date(2025, 8, 1), "biomass", 1.0, 0.1))


# --- 31 Oct 2026 fixes: parameter write-back, real inflation, cover/stage operators ---

def _potato_cfg():
    return ZoneConfig(zone_id="p", latitude=28.45, longitude=77.58, crop="potato",
                      variety="Potato_701", sowing_date=dt.date(2025, 10, 25),
                      harvest_date=dt.date(2026, 2, 10))


@pytest.fixture(scope="module")
def bennett_weather():
    return make_synthetic_weather(28.45, 77.58, dt.date(2025, 10, 1), days=200, seed=3)


def _pert():
    return ParameterPerturbation(TSUM1=0.2, TSUM2=0.2, SPAN=0.2, RGRLAI=0.2,
                                 TDWI=0.2, WAV=0.0, NAVAILI=0.0)


def test_parameter_update_reaches_the_running_crop(bennett_weather):
    """The bug: updated factors used to live only in a bookkeeping dict."""
    ens = EnsembleTwin(_potato_cfg(), bennett_weather, n_members=12, seed=4,
                       perturbation=_pert(), engine="Wofost72_WLP_CWB")
    day = dt.date(2025, 12, 5)
    ens.run_till(day)
    before = [ens.current_parameters(i)["TSUM2"] for i in range(ens.n)]
    dvs = ens._augmented()[1]
    ens.assimilate(Observation(day, "DVS", float(dvs.mean() + 2 * dvs.std()), 0.03))
    after = [ens.current_parameters(i)["TSUM2"] for i in range(ens.n)]
    assert any(abs(a - b) > 1e-6 for a, b in zip(after, before))
    for i, m in enumerate(ens.members):        # running crop == bookkeeping
        assert ens.current_parameters(i)["TSUM2"] == pytest.approx(m._base["TSUM2"] * m._perturb["TSUM2"])


def test_inflation_widens_the_posterior(bennett_weather):
    """The bug: inflation was divided back out in the same expression."""
    day = dt.date(2025, 12, 1)
    sds = []
    for lam in (1.0, 1.3):
        ens = EnsembleTwin(_potato_cfg(), bennett_weather, n_members=12, seed=5,
                           perturbation=_pert(), engine="Wofost72_WLP_CWB", inflation=lam)
        ens.run_till(day)
        lai = ens._augmented()[0]
        # A weak observation, so the posterior is mostly the (inflated) prior.
        ens.assimilate(Observation(day, "LAI", float(lai.mean()), 1.5))
        sds.append(float(ens._augmented()[0].std()))
    assert sds[1] > sds[0] * 1.15


def test_canopy_cover_operator_moves_lai_the_right_way(bennett_weather):
    ens = EnsembleTwin(_potato_cfg(), bennett_weather, n_members=12, seed=6,
                       perturbation=_pert(), engine="Wofost72_WLP_CWB")
    day = dt.date(2025, 12, 1)
    ens.run_till(day)
    lai = ens._augmented()[0].mean()
    cover = 1 - np.exp(-0.6 * lai)
    ens.assimilate(Observation(day, "CC", float(min(0.97, cover + 0.15)), 0.04))
    assert ens._augmented()[0].mean() > lai


def test_cover_and_stage_are_assimilated_jointly(bennett_weather):
    ens = EnsembleTwin(_potato_cfg(), bennett_weather, n_members=10, seed=7,
                       perturbation=_pert(), engine="Wofost72_WLP_CWB")
    day = dt.date(2025, 12, 1)
    ens.run_till(day)
    X = ens._augmented()
    innov, rejected = ens.assimilate([
        Observation(day, "CC", float(1 - np.exp(-0.6 * X[0].mean())), 0.04),
        Observation(day, "DVS", float(X[1].mean()), 0.05)])
    assert not rejected


def test_per_member_weather_is_supported(bennett_weather):
    from fa_twin.weather import forecast_ensemble, records_provider
    ens_recs = forecast_ensemble(bennett_weather.records, dt.date(2025, 11, 20), 4, seed=1)
    ws = [records_provider(r, 28.45, 77.58) for r in ens_recs]
    ens = EnsembleTwin(_potato_cfg(), bennett_weather, n_members=4, seed=8,
                       engine="Wofost72_WLP_CWB", weathers=ws)
    ens.run([])
    _, sd, vals = ens.yield_distribution()
    assert len(vals) == 4 and np.all(vals > 0)


def test_finite_ensemble_interval_holds_its_level():
    """Order-statistic interval: exchangeable truth covered >= level; percentile one is not."""
    from fa_twin.assimilation import EnsembleTwin as ET
    rng = np.random.default_rng(0)
    hits_os = hits_pct = 0
    trials = 4000
    for _ in range(trials):
        vals = rng.normal(0, 1, 24)
        truth = rng.normal(0, 1)
        fake = ET.__new__(ET)
        fake.yield_distribution = lambda v=vals: (v.mean(), v.std(), v)
        lo, hi, lvl = fake.interval(0.8)
        hits_os += lo <= truth <= hi
        p10, p90 = np.percentile(vals, [10, 90])
        hits_pct += p10 <= truth <= p90
    assert lvl >= 0.8
    assert hits_os / trials >= 0.79
    assert hits_pct / trials < 0.77            # the naive interval undercovers


def test_growth_stage_observation_corrects_the_current_stage(bennett_weather):
    """Review #3: a DVS observation must move the ensemble's DVS now, not only future rates."""
    ens = EnsembleTwin(_potato_cfg(), bennett_weather, n_members=16, seed=9,
                       perturbation=_pert(), engine="Wofost72_WLP_CWB")
    day = dt.date(2025, 12, 9)
    ens.run_till(day)
    dvs = ens._augmented()[1]
    target = float(dvs.mean() + 1.5 * dvs.std())
    ens.assimilate(Observation(day, "DVS", target, 0.03))
    post = ens._augmented()[1]
    assert abs(post.mean() - target) < 0.6 * abs(dvs.mean() - target)
    assert all(m.day == day for m in ens.members)          # members are back at the obs day
