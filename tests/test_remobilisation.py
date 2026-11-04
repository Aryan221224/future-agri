"""Stem-reserve remobilisation (CERES-Maize rule in WOFOST 7.2) and the canopy-size satellite anchor."""
import datetime as dt

import pytest

from fa_twin.engine import SoilParams, ZoneConfig, ZoneTwin
from fa_twin.management import leaf_share_overrides
from fa_twin.remobilisation import STEM_FLOOR
from fa_twin.weather import generate_records, records_provider

LAT, LON = 41.18, -96.44


def _normals(summer_rain_mm):
    return {m: (28 if 5 <= m <= 9 else 5, 15 if 5 <= m <= 9 else -5,
                summer_rain_mm if 7 <= m <= 9 else 60, 9, 22 if 5 <= m <= 8 else 8,
                18 if 5 <= m <= 9 else 5, 3.5) for m in range(1, 13)}


def _run(remob, summer_rain_mm=5, engine="Wofost72_WLP_CWB", wav=8.0):
    recs = generate_records(dt.date(2004, 1, 1), 366, seed=3, normals=_normals(summer_rain_mm))
    cfg = ZoneConfig("z", LAT, LON, "maize", "Grain_maize_204", dt.date(2004, 5, 10), dt.date(2004, 10, 15),
                     soil=SoilParams(SMFCF=0.30, SMW=0.12, SM0=0.45, RDMSOL=120), WAV=wav, remobilisation=remob)
    tw = ZoneTwin(cfg, records_provider(recs, LAT, LON), engine=engine)
    tw.run_to_end()
    return tw, tw.output_frame()


@pytest.fixture(scope="module")
def drought():
    return _run(False), _run(True)


def test_drought_moves_stem_to_grain_and_conserves_biomass(drought):
    (base, b), (rem, r) = drought
    moved = rem.summary()["remobilised_kg_ha"]
    assert moved > 100
    assert r["TWSO"].iloc[-1] > b["TWSO"].iloc[-1] + 0.5 * moved
    assert r["TWST"].iloc[-1] < b["TWST"].iloc[-1]
    # dry matter is moved, not made: above-ground total stays within 3 % (only respiration differs)
    assert r["TAGP"].iloc[-1] == pytest.approx(b["TAGP"].iloc[-1], rel=0.03)


def test_stem_never_drops_below_the_ceres_floor(drought):
    _, (rem, r) = drought
    silk = r[r["DVS"] >= 1.0]
    floor = STEM_FLOOR * float(silk["TWST"].iloc[0])
    assert float(silk["TWST"].min()) >= floor - 1.0


def test_no_water_stress_changes_nothing():
    # potential production: the flag must be harmless
    (_, b), (_, r) = _run(False, 5, "Wofost72_PP", wav=30.0), _run(True, 5, "Wofost72_PP", wav=30.0)
    assert r["TWSO"].iloc[-1] == pytest.approx(b["TWSO"].iloc[-1], rel=1e-9)
    # water-limited: nothing moves on a day without water stress
    (_, b), (rem, r) = _run(False, 120, wav=30.0), _run(True, 120, wav=30.0)
    fill = b[b["DVS"] >= 1.0]
    if (fill["RFTRA"] >= 1.0).all():
        assert rem.summary()["remobilised_kg_ha"] == 0.0
        assert r["TWSO"].iloc[-1] == pytest.approx(b["TWSO"].iloc[-1], rel=1e-9)
    else:   # stressed days only: remobilisation can only add grain
        assert r["TWSO"].iloc[-1] >= b["TWSO"].iloc[-1]


def test_flag_off_is_the_shipped_model():
    tw, _ = _run(False)
    assert type(tw.model).__name__ == "Wofost72_WLP_CWB"
    assert "remobilised_kg_ha" not in tw.summary()


def test_leaf_share_scaling_keeps_shoot_allocation_and_bounds():
    t = {"FLTB": [0.0, 0.62, 0.88, 0.15, 2.0, 0.0], "FSTB": [0.0, 0.38, 0.88, 0.85, 2.0, 0.0]}
    up = leaf_share_overrides(t, 1.5)
    assert up["FLTB"][1] == pytest.approx(min(0.62 * 1.5, 1.0))
    for i in range(1, len(t["FLTB"]), 2):
        assert up["FLTB"][i] + up["FSTB"][i] == pytest.approx(t["FLTB"][i] + t["FSTB"][i], abs=1e-6)
        assert up["FSTB"][i] >= -1e-9
    assert leaf_share_overrides(t, 9.0) == leaf_share_overrides(t, 1.6)


def test_canopy_size_anchor_grows_a_canopy_the_satellite_sees_as_larger():
    from fa_farm.anchor import SATELLITE, anchor_to_cover
    from fa_twin.assimilation import K_FVC
    from fa_twin.hybrids import crop_table
    import math
    crop = crop_table("maize", "Grain_maize_204")
    recs = generate_records(dt.date(2004, 1, 1), 366, seed=3, normals=_normals(90))
    cfg = ZoneConfig("z", LAT, LON, "maize", "Grain_maize_204", dt.date(2004, 5, 10), dt.date(2004, 10, 15),
                     soil=SoilParams(SMFCF=0.30, SMW=0.12, SM0=0.45, RDMSOL=120), WAV=20.0,
                     param_overrides=leaf_share_overrides(crop, 0.6))
    tw = ZoneTwin(cfg, records_provider(recs, LAT, LON), engine="Wofost72_PP")
    days = [dt.date(2004, 6, 15) + dt.timedelta(days=10 * k) for k in range(7)]
    lai = []
    for d in days:
        tw.run_till(d)
        lai.append(float(tw.state().get("LAI") or 0.0))
    # the "field" carries twice the model's leaf area
    series = [(d, None, 1 - math.exp(-K_FVC * 2.0 * v)) for d, v in zip(days, lai)]
    ov, info = anchor_to_cover(cfg, recs, series, members=16, seed=1, engine="Wofost72_PP", **SATELLITE)
    assert info["leaf_share"] > 1.1
    assert "FLTB" in ov and "FSTB" in ov
