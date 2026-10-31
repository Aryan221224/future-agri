"""Weather generator and forecast ensemble.

The audit's first finding was that every benchmark ran on 102 mm of rain a
year. These tests pin the generator to the station normals so that can never
silently happen again.
"""
import datetime as dt

import numpy as np
import pytest

from fa_twin.weather import (DELHI_NORMALS, RAIN_CAP_MM, annual_totals, forecast_ensemble,
                             generate_records, make_synthetic_weather)

NORMAL_RAIN = sum(v[2] for v in DELHI_NORMALS.values())          # 774 mm
NORMAL_JJAS = sum(DELHI_NORMALS[m][2] for m in (6, 7, 8, 9))     # 640 mm


@pytest.fixture(scope="module")
def decade_stats():
    return [annual_totals(generate_records(dt.date(2000, 1, 1), 3653, s)) for s in range(12)]


def test_annual_rain_matches_normals(decade_stats):
    mean = np.mean([t["rain_mm_per_year"] for t in decade_stats])
    assert abs(mean - NORMAL_RAIN) / NORMAL_RAIN < 0.12, mean


def test_monsoon_carries_the_rain(decade_stats):
    jjas = np.mean([t["jjas_mm_per_year"] for t in decade_stats])
    assert abs(jjas - NORMAL_JJAS) / NORMAL_JJAS < 0.12, jjas


def test_rain_is_capped_and_diurnal_range_realistic(decade_stats):
    assert max(t["max_daily_mm"] for t in decade_stats) <= RAIN_CAP_MM
    dtr = np.mean([t["mean_dtr_c"] for t in decade_stats])
    assert 9.0 < dtr < 15.0


def test_winter_is_cool_and_dry():
    recs = generate_records(dt.date(2025, 12, 1), 62, seed=3)
    tmax = np.mean([r["TMAX"] for r in recs])
    assert 17 < tmax < 26
    assert sum(r["RAIN"] for r in recs) < 120


def test_vapour_pressure_never_exceeds_saturation():
    for r in generate_records(dt.date(2025, 1, 1), 365, seed=5):
        es = 6.108 * np.exp(17.27 * r["TMIN"] / (r["TMIN"] + 237.3))
        assert r["VAP"] <= es + 1e-9


def test_provider_is_deterministic_and_stamped():
    a = make_synthetic_weather(28.45, 77.58, dt.date(2025, 10, 1), days=60, seed=9)
    b = make_synthetic_weather(28.45, 77.58, dt.date(2025, 10, 1), days=60, seed=9)
    d = dt.date(2025, 10, 20)
    assert a(d).TMAX == b(d).TMAX and a(d).RAIN == b(d).RAIN
    assert "NOT-FOR-ADVICE" in str(a)
    assert a(d).RAIN == pytest.approx(a.records[19]["RAIN"] / 10.0)   # cm in PCSE, mm in records


def test_forecast_keeps_past_and_loses_skill_with_lead():
    truth = generate_records(dt.date(2025, 10, 1), 120, seed=11)
    issue = dt.date(2025, 11, 1)
    ens = forecast_ensemble(truth, issue, n_members=30, seed=2)
    i0 = (issue - truth[0]["DAY"]).days
    for m in ens:                                   # the past is observed, identical
        assert all(m[i] == truth[i] for i in range(i0))
    err = lambda lead: np.mean([abs(m[i0 + lead]["TMAX"] - truth[i0 + lead]["TMAX"]) for m in ens])
    assert err(1) < err(10)
    # beyond the forecast horizon members are independent climatology
    tail = [m[i0 + 40]["TMAX"] for m in ens]
    assert np.std(tail) > 1.0


def test_forecast_ensemble_is_reproducible():
    """Common random numbers: the same seed must give the same members."""
    truth = generate_records(dt.date(2025, 10, 1), 90, seed=1)
    a = forecast_ensemble(truth, dt.date(2025, 10, 20), 5, seed=4)
    b = forecast_ensemble(truth, dt.date(2025, 10, 20), 5, seed=4)
    assert a == b
