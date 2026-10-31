"""Task 2: real weather history and real archived ensemble forecasts."""
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pytest

from fa_farm.sources.base import Cache
from fa_farm.sources import weather as W
from fa_farm.sources import forecasts as F

FIX = Path(__file__).parent / "fixtures" / "farm"


def _power_records():
    raw = json.loads((FIX / "power_nl_202504.json").read_text())
    return W.parse_power(raw)


def test_power_parse_fixture_units():
    recs, elev = _power_records()
    assert len(recs) == 30
    r = recs[15]
    assert r["DAY"] == dt.date(2025, 4, 16)
    assert 1e6 < r["IRRAD"] < 3.5e7          # J/m2/day
    assert 3.0 < r["VAP"] < 30.0             # hPa
    assert r["TMIN"] <= r["TMAX"]
    assert 0.0 <= r["RAIN"] < 100.0          # mm
    assert 0.0 <= r["RH"] <= 100.0
    assert -10 < elev < 50                   # Flevoland is near sea level


def test_power_daily_uses_cache(tmp_path, monkeypatch):
    raw = json.loads((FIX / "power_nl_202504.json").read_text())
    c = Cache(tmp_path)
    monkeypatch.setattr(W, "fetch_json", lambda url, params, cache, layer, timeout=120: raw)
    recs, prov, elev = W.power_daily(52.5, 5.6, dt.date(2025, 4, 1), dt.date(2025, 4, 30), c)
    assert len(recs) == 30 and "NASA" in prov.source and prov.commercial_use


def test_monthly_climatology():
    recs, _ = _power_records()
    clim = W.monthly_climatology(recs)
    assert set(clim) == {4}
    assert clim[4]["TMAX"] > clim[4]["TMIN"]


def test_available_sources_dates():
    assert F.ForecastSource.IFS_ENS not in F.available_sources(dt.date(2024, 3, 1))
    s = F.available_sources(dt.date(2025, 8, 1))
    assert s[:3] == [F.ForecastSource.IFS_ENS, F.ForecastSource.AIFS_ENS, F.ForecastSource.GEFS]
    assert F.available_sources(dt.date(2019, 6, 1)) == [F.ForecastSource.CLIMATOLOGY]


def test_daily_members_from_real_ifs_fixture():
    z = np.load(FIX / "ifs_nl_20250602.npz")
    arrs = {k: z[k] for k in z.files}
    members = F.daily_members(arrs, dt.date(2025, 6, 2), F.ForecastSource.IFS_ENS)
    assert len(members) == 51
    assert len(members[0]) == 15
    m0 = members[0]
    assert m0[0]["DAY"] == dt.date(2025, 6, 2) and m0[-1]["DAY"] == dt.date(2025, 6, 16)
    for r in m0:
        assert r["TMIN"] <= r["TMAX"] and 0 <= r["RAIN"] < 200
        assert 1e5 < r["IRRAD"] < 3.6e7 and 2 < r["VAP"] < 35 and 0 <= r["WIND"] < 25
    # members differ (it is an ensemble) but share the first-day weather closely
    tmax_day10 = [m[10]["TMAX"] for m in members]
    assert np.std(tmax_day10) > 0.3


def test_fallback_named_when_archive_missing(tmp_path):
    """Review focus 2: no archive for the issue date -> climatology, and the
    report says which source was used."""
    hist = []
    for y in range(2015, 2019):
        d = dt.date(y, 1, 1)
        while d.year == y:
            hist.append({"DAY": d, "TMAX": 15.0, "TMIN": 5.0, "RAIN": 1.0, "IRRAD": 1.2e7,
                         "VAP": 10.0, "WIND": 3.0, "RH": 80.0})
            d += dt.timedelta(days=1)
    members, src, prov = F.ensemble_forecast(52.5, 5.6, dt.date(2019, 6, 1), Cache(tmp_path),
                                             history=hist, n_members=8)
    assert src is F.ForecastSource.CLIMATOLOGY
    assert "climatology" in prov.source.lower()
    assert len(members) == 8 and len(members[0]) == 15
    assert members[0][0]["DAY"] == dt.date(2019, 6, 1)


def test_bias_corrector_removes_warm_bias():
    obs = [{"DAY": dt.date(2025, 6, i), "TMAX": 20.0, "TMIN": 10.0, "RAIN": 2.0,
            "IRRAD": 2e7, "VAP": 12.0, "WIND": 3.0} for i in range(1, 21)]
    fc = [dict(r, TMAX=r["TMAX"] + 2.0, TMIN=r["TMIN"] + 2.0, RAIN=r["RAIN"] * 2) for r in obs]
    bc = F.BiasCorrector()
    bc.update(fc, obs)
    out = bc.apply([[dict(r) for r in fc]])
    assert abs(np.mean([r["TMAX"] for r in out[0]]) - 20.0) < 0.1
    assert abs(np.mean([r["RAIN"] for r in out[0]]) - 2.0) < 0.1
    # no pairs -> identity
    assert F.BiasCorrector().apply([[dict(fc[0])]])[0][0]["TMAX"] == fc[0]["TMAX"]


def test_season_members_join_observed_forecast_and_tail():
    hist = []
    for y in range(2015, 2025):
        d = dt.date(y, 1, 1)
        while d.year == y:
            hist.append({"DAY": d, "TMAX": 18.0 + y % 3, "TMIN": 8.0, "RAIN": 1.0,
                         "IRRAD": 1.5e7, "VAP": 11.0, "WIND": 3.0})
            d += dt.timedelta(days=1)
    issue, end = dt.date(2025, 6, 2), dt.date(2025, 9, 1)
    fc = [[{"DAY": issue + dt.timedelta(days=i), "TMAX": 25.0, "TMIN": 12.0, "RAIN": 0.0,
            "IRRAD": 2e7, "VAP": 13.0, "WIND": 2.0} for i in range(15)] for _ in range(3)]
    mem = F.season_members(fc, hist, end, seed=1)
    assert len(mem) == 3
    days = [r["DAY"] for r in mem[0]]
    assert days[0] == issue and days[-1] == end and len(days) == (end - issue).days + 1
    assert mem[0][3]["TMAX"] == 25.0                   # forecast part kept
    assert mem[0][40]["DAY"].year == 2025              # tail re-dated to the target year


@pytest.mark.network
def test_ifs_ens_point_read_network(tmp_path):
    arrs = F.read_point(F.ForecastSource.IFS_ENS, dt.date(2025, 6, 2), 52.5, 5.6, Cache(tmp_path))
    assert arrs["temperature_2m"].shape == (85, 51)
