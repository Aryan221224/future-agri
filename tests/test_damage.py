"""Indian wheat variety set and the blight/weed damage layer."""
import datetime as dt

import numpy as np
import pytest

from fa_twin.damage import (Blight, BlightParams, economic_threshold, favourable,
                            infection_day, run_with_damage, weed_loss_frac)
from fa_twin.engine import ZoneConfig, ZoneTwin, check_photoperiod
from fa_twin.weather import generate_records, records_provider

LAT, LON = 28.45, 77.58


def _wheat(variety):
    return ZoneConfig(zone_id="w", latitude=LAT, longitude=LON, crop="wheat", variety=variety,
                      sowing_date=dt.date(2025, 11, 15), harvest_date=dt.date(2026, 4, 30))


def _irrigation(start, every, cm, n):
    return [{"date": start + dt.timedelta(days=every * (i + 1)), "irrigation_cm": cm}
            for i in range(n)]


def test_european_wheat_is_refused_at_bennett():
    assert not check_photoperiod(_wheat("Winter_wheat_102"))


def test_indian_wheat_set_passes_the_gate_and_matures():
    cfg = _wheat("IN_NW_timely")
    assert check_photoperiod(cfg)
    w = records_provider(generate_records(dt.date(2025, 11, 1), 220, seed=101), LAT, LON)
    tw = ZoneTwin(cfg, w, events=_irrigation(cfg.sowing_date, 20, 6.0, 6),
                  engine="Wofost72_WLP_CWB")
    tw.run_to_end()
    s = tw.summary()
    assert s["DOM"] is not None, "never reached maturity"
    assert 120 <= (s["DOM"] - cfg.sowing_date).days <= 160
    assert 3000 < s["TWSO"] < 7000
    assert "NOT a yield calibration" in s["parameter_provenance"]


def _potato(seed=7):
    recs = generate_records(dt.date(2025, 10, 1), 200, seed=seed)
    cfg = ZoneConfig(zone_id="p", latitude=LAT, longitude=LON, crop="potato",
                     variety="Potato_701", sowing_date=dt.date(2025, 10, 25),
                     harvest_date=dt.date(2026, 2, 10))
    events = _irrigation(cfg.sowing_date, 10, 4.0, 10)
    mk = lambda: ZoneTwin(cfg, records_provider(recs, LAT, LON), events=events,
                          engine="Wofost72_WLP_CWB")
    return recs, cfg, mk


def test_blight_destroys_yield_and_protectant_sprays_save_it():
    recs, cfg, mk = _potato()
    inf = cfg.sowing_date + dt.timedelta(days=40)
    healthy = run_with_damage(mk(), recs, Blight(None)).TWSO
    sick = run_with_damage(mk(), recs, Blight(inf)).TWSO
    weekly = [cfg.sowing_date + dt.timedelta(days=d) for d in range(35, 108, 7)]
    sprayed = run_with_damage(mk(), recs, Blight(inf), spray_days=weekly).TWSO
    assert sick < 0.85 * healthy
    assert sprayed > sick


def test_rain_washes_off_protectant():
    b = Blight(dt.date(2025, 12, 1))
    b.spray(dt.date(2025, 12, 1))
    b.step(dict(DAY=dt.date(2025, 12, 2), TMAX=20, TMIN=10, RAIN=15.0, VAP=12.0))
    assert not b.protected(dt.date(2025, 12, 3))


def test_inoculum_alert_controls_infection():
    recs = generate_records(dt.date(2025, 10, 1), 200, seed=2)
    plant = dt.date(2025, 10, 25)
    assert infection_day(recs, plant, inoculum=False) is None
    d = infection_day(recs, plant, inoculum=True)
    assert d is None or d >= plant + dt.timedelta(days=BlightParams().earliest_day)


def test_favourable_needs_mild_wet_conditions():
    assert favourable(dict(TMAX=20, TMIN=10, RAIN=5.0, VAP=10.0))
    assert not favourable(dict(TMAX=38, TMIN=26, RAIN=5.0, VAP=30.0))     # too hot
    assert not favourable(dict(TMAX=20, TMIN=10, RAIN=0.0, VAP=6.0))      # dry night


def test_cousens_weed_loss_and_threshold():
    assert weed_loss_frac(0) == 0
    assert weed_loss_frac(12) == pytest.approx(0.115, abs=0.01)    # audit: ~12 weeds/m2 -> ~-12 %
    assert weed_loss_frac(1e6) == pytest.approx(0.55, abs=0.01)    # saturates at A
    # potato ~24 t/ha fresh at Rs 10/kg, blanket herbicide Rs 2000/ha -> ~1 weed/m2 (audit 1.1)
    thr = economic_threshold(10.0, 24_400, 2000.0)
    assert 0.6 < thr < 1.6


def test_nitrogen_gate_refuses_potato_81_and_passes_indian_wheat():
    """Shipped 8.1 potato cannot be N-limited (40 kg uptake, ~ceiling yield); wheat can."""
    from fa_twin.engine import check_nitrogen_response
    w = records_provider(generate_records(dt.date(2025, 10, 1), 220, seed=700), LAT, LON)
    _, pcfg, _ = _potato(700)
    pirr = _irrigation(pcfg.sowing_date, 10, 4.0, 10)
    g = check_nitrogen_response(pcfg, w, pirr)
    assert not g.ok and "cannot be used for nitrogen advice" in g.reasons[0]
    wcfg = _wheat("IN_NW_timely")
    ww = records_provider(generate_records(dt.date(2025, 11, 1), 220, seed=700), LAT, LON)
    assert check_nitrogen_response(wcfg, ww, _irrigation(wcfg.sowing_date, 20, 6.0, 6)).ok
