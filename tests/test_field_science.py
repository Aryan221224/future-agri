"""Field science layer (validated at Mead, Nebraska): gauge weather, soil survey, stand,
residue, soil water at sowing. All offline -- sources are fed through the cache or patched."""
import datetime as dt

import numpy as np
import pytest

from fa_farm.sources.base import Cache
from fa_twin.engine import SoilParams
from fa_twin.management import (population_overrides, residue_cover, soil_evaporation_factor)
from fa_twin.weather import generate_records, records_provider

NORMALS = {m: (28 if 5 <= m <= 9 else 5, 15 if 5 <= m <= 9 else -5, 90 if 5 <= m <= 8 else 30, 9,
               22 if 5 <= m <= 8 else 8, 18 if 5 <= m <= 9 else 5, 3.5) for m in range(1, 13)}


def _records(start=dt.date(2003, 1, 1), days=730, seed=1):
    return generate_records(start, days, seed=seed, normals=NORMALS)


MAIZE = {"TDWI": 50.0, "FLTB": [0.0, 0.62, 0.33, 0.62, 0.88, 0.15, 1.2, 0.0, 2.0, 0.0],
         "FSTB": [0.0, 0.38, 0.33, 0.38, 0.88, 0.85, 1.2, 0.0, 2.0, 0.0]}


# ---------------------------------------------------------------- stand and residue
def test_population_scales_seed_weight_and_leaf_share_conserving_shoot_allocation():
    ov = population_overrides("maize", MAIZE, 56000)          # 0.70 of the 80 000 reference
    r = 56000 / 80000
    assert ov["TDWI"] == pytest.approx(50.0 * r)
    fl, fs = ov["FLTB"], ov["FSTB"]
    for i in range(0, len(fl), 2):
        assert fl[i] == MAIZE["FLTB"][i]
        assert fl[i + 1] == pytest.approx(MAIZE["FLTB"][i + 1] * r, abs=1e-4)
        assert fl[i + 1] + fs[i + 1] == pytest.approx(MAIZE["FLTB"][i + 1] + MAIZE["FSTB"][i + 1], abs=1e-4)


def test_population_at_reference_or_unknown_changes_nothing_and_extremes_are_bounded():
    assert population_overrides("maize", MAIZE, 80000) == {}
    assert population_overrides("maize", MAIZE, None) == {}
    assert population_overrides("potato", MAIZE, 40000) == {}        # no reference stand for potato
    assert population_overrides("maize", MAIZE, 5000)["TDWI"] == pytest.approx(50.0 * 0.4)


def test_population_refuses_mismatched_tables():
    bad = dict(MAIZE, FSTB=[0.0, 0.38, 0.5, 0.38, 2.0, 0.0])
    with pytest.raises(ValueError):
        population_overrides("maize", bad, 56000)


def test_residue_cover_and_fao56_evaporation_factor():
    assert residue_cover(None, None) == 0.0
    assert residue_cover("no-till", "soybean") == 0.50
    assert residue_cover("no-till", "maize") == 0.70
    assert residue_cover("conventional", "maize") == 0.10
    assert soil_evaporation_factor(0.0) == 1.0
    assert soil_evaporation_factor(0.5) == 0.75                       # 5 % less per 10 % covered
    assert soil_evaporation_factor(1.5) == 0.5


def test_records_provider_scales_only_bare_soil_evaporation():
    recs = _records(days=10)
    a = records_provider(recs, 41.0, -96.5, 350.0)
    b = records_provider(recs, 41.0, -96.5, 350.0, soil_evap_factor=0.6)
    d = recs[5]["DAY"]
    wa, wb = a(d), b(d)
    assert wb.ES0 == pytest.approx(0.6 * wa.ES0)
    assert wb.ET0 == pytest.approx(wa.ET0) and wb.E0 == pytest.approx(wa.E0)
    b.extend([dict(recs[5], RAIN=20.0)])                              # days added later keep the factor
    assert b(d).ES0 == pytest.approx(0.6 * wa.ES0)


# ---------------------------------------------------------------- soil water at sowing
def test_residue_keeps_more_water_in_the_profile_over_winter():
    from fa_twin.soilwater import available_water_at_sowing
    recs = _records()
    bare = available_water_at_sowing(recs, 0.36, 0.19, dt.date(2004, 5, 10), 150, lat=41.0, elev=350)
    mulch = available_water_at_sowing(recs, 0.36, 0.19, dt.date(2004, 5, 10), 150, lat=41.0, elev=350,
                                      evap_factor=0.75)
    assert 0 < bare["WAV_cm"] <= bare["capacity_cm"]
    assert mulch["WAV_cm"] >= bare["WAV_cm"]


# ---------------------------------------------------------------- rain gauge
def _gauge(monkeypatch, obs, km=5.3):
    from fa_farm.sources import stations
    st = {"id": "USC00255362", "name": "MEAD 6 S, NE US", "lat": 41.14, "lon": -96.48, "start": "1968-01-01",
          "end": "2026-10-31", "km": km}
    monkeypatch.setattr(stations, "nearest_stations", lambda *a, **k: [st] if km <= 15 else [])
    monkeypatch.setattr(stations, "station_daily", lambda *a, **k: obs)
    return stations


def test_gauge_replaces_rain_and_temperature_but_rejects_impossible_values(monkeypatch, tmp_path):
    recs = _records(days=60)
    obs = {r["DAY"].isoformat(): {"PRCP": 0.0, "TMAX": r["TMAX"] - 1.0, "TMIN": r["TMIN"] - 1.0} for r in recs}
    d0, d1 = recs[3]["DAY"].isoformat(), recs[4]["DAY"].isoformat()
    obs[d0] = {"PRCP": 42.0, "TMAX": 10.0, "TMIN": 20.0}             # min above max: temperature rejected
    obs[d1] = {"PRCP": None, "TMAX": None, "TMIN": None}              # gauge silent: gridded day stands
    st = _gauge(monkeypatch, obs)
    out, prov, info = st.merge_station(recs, 41.17, -96.44, recs[0]["DAY"], recs[-1]["DAY"], Cache(tmp_path))
    assert prov is not None and info["station"]["id"] == "USC00255362"
    assert out[3]["RAIN"] == 42.0 and out[3]["TMAX"] == recs[3]["TMAX"]
    assert out[4] == recs[4]
    assert out[10]["RAIN"] == 0.0 and out[10]["TMAX"] == pytest.approx(recs[10]["TMAX"] - 1.0)
    assert recs[10]["RAIN"] == _records(days=60)[10]["RAIN"]           # the caller's records are not mutated


def test_gauge_with_too_little_data_is_not_used(monkeypatch, tmp_path):
    recs = _records(days=60)
    obs = {r["DAY"].isoformat(): {"PRCP": 1.0, "TMAX": 20.0, "TMIN": 10.0} for r in recs[:20]}
    st = _gauge(monkeypatch, obs)
    out, prov, info = st.merge_station(recs, 41.17, -96.44, recs[0]["DAY"], recs[-1]["DAY"], Cache(tmp_path))
    assert prov is None and out is recs and "no GHCN station" in info["note"]


def test_gauge_search_failure_keeps_gridded_weather(monkeypatch, tmp_path):
    from fa_farm.sources import stations

    def boom(*a, **k):
        raise ConnectionError("offline")
    monkeypatch.setattr(stations, "nearest_stations", boom)
    recs = _records(days=10)
    out, prov, info = stations.merge_station(recs, 41.0, -96.0, recs[0]["DAY"], recs[-1]["DAY"], Cache(tmp_path))
    assert out is recs and prov is None and "offline" in info["note"]


# ---------------------------------------------------------------- soil survey
TOMEK = [  # NRCS SSURGO Tomek silt loam (Mead, NE) -- shape of the SDA rows
    {"muname": "Tomek silt loam, 0 to 2 percent slopes", "compname": "Tomek", "comppct_r": 85, "hzdept_r": 0,
     "hzdepb_r": 15, "awc_r": 0.22, "wthirdbar_r": 33.0, "wfifteenbar_r": 15.0, "wsatiated_r": 48,
     "ksat_r": 9.0, "sandtotal_r": 5, "claytotal_r": 25, "om_r": 3},
    {"muname": "Tomek silt loam, 0 to 2 percent slopes", "compname": "Tomek", "comppct_r": 85, "hzdept_r": 15,
     "hzdepb_r": 70, "awc_r": 0.18, "wthirdbar_r": 34.0, "wfifteenbar_r": 21.0, "wsatiated_r": 46,
     "ksat_r": 3.0, "sandtotal_r": 4, "claytotal_r": 38, "om_r": 1},
    {"muname": "Tomek silt loam, 0 to 2 percent slopes", "compname": "Tomek", "comppct_r": 85, "hzdept_r": 70,
     "hzdepb_r": 200, "awc_r": 0.17, "wthirdbar_r": 31.0, "wfifteenbar_r": 18.0, "wsatiated_r": 45,
     "ksat_r": 9.0, "sandtotal_r": 6, "claytotal_r": 29, "om_r": 0.5},
]


def test_ssurgo_rootzone_uses_survey_awc_depth_weighted(tmp_path):
    from fa_farm.sources.ssurgo import ssurgo_rootzone
    c = Cache(tmp_path)
    c.put_json(c.key("ssurgo", 41.18, -96.44), TOMEK)
    rows, sp, prov = ssurgo_rootzone(41.18, -96.44, c, depth_cm=150)
    wp = (15 * 0.15 + 55 * 0.21 + 80 * 0.18) / 150
    awc = (15 * 0.22 + 55 * 0.18 + 80 * 0.17) / 150
    assert sp.SMW == pytest.approx(wp, abs=1e-4)
    assert sp.SMFCF == pytest.approx(wp + awc, abs=1e-4)
    assert sp.RDMSOL == 150.0 and sp.SM0 > sp.SMFCF
    assert "Tomek" in prov.as_dict()["source"] if "source" in prov.as_dict() else "Tomek" in str(prov.as_dict())


def test_ssurgo_shallow_profile_is_refused(tmp_path):
    from fa_farm.sources.base import DataUnavailable
    from fa_farm.sources.ssurgo import ssurgo_rootzone
    c = Cache(tmp_path)
    c.put_json(c.key("ssurgo", 41.0, -96.0), TOMEK[:1])
    with pytest.raises(DataUnavailable):
        ssurgo_rootzone(41.0, -96.0, c, depth_cm=150)


# ---------------------------------------------------------------- the layer as a whole
def test_derive_offline_keeps_priors_and_adds_physics(monkeypatch, tmp_path):
    from fa_farm import science
    from fa_farm.sources import soil as soil_src
    from fa_farm.sources import ssurgo, stations
    monkeypatch.setattr(stations, "nearest_stations", lambda *a, **k: [])
    monkeypatch.setattr(ssurgo, "_query", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("offline")))
    monkeypatch.setattr(soil_src, "soilgrids_rootzone", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x")))
    prior = SoilParams(SMFCF=0.36, SMW=0.19, RDMSOL=150.0)
    recs = _records()
    fs = science.derive(41.17, -96.44, "maize", "Grain_maize_204", dt.date(2004, 5, 12), recs, Cache(tmp_path),
                        prior, 350.0, {"population": 56000, "tillage": "no-till", "previous_crop": "soybean",
                                       "gdu_black_layer": 2400})
    assert fs.soil is prior
    assert fs.overrides["RRI"] == 3.1 and fs.overrides["RDMCR"] == 150.0
    assert fs.overrides["TDWI"] < 50.0 and "FLTB" in fs.overrides
    assert fs.overrides["TSUM1"] > 0 and fs.overrides["TSUM2"] > 0
    assert fs.soil_evap_factor == 0.75
    assert fs.WAV is not None and 0 < fs.WAV <= (0.36 - 0.19) * 150
    assert any("rain gauge" in w for w in fs.warnings) and any("root-zone soil" in w for w in fs.warnings)
    late = science.derive(41.17, -96.44, "maize", "Grain_maize_204", dt.date(2004, 5, 12), recs, Cache(tmp_path),
                          prior, 350.0, {"gdu_black_layer": 3200})        # never matures in this climate
    assert "TSUM1" not in late.overrides and any("hybrid rating not converted" in w for w in late.warnings)


def test_spec_carries_science_to_truth_zones():
    from fa_farm.archetypes import load_archetypes
    from fa_farm.truth import FarmSpec
    from shapely.geometry import box
    a = load_archetypes()["A5"]
    soil = SoilParams(SMFCF=0.36, SMW=0.19, RDMSOL=150.0)
    spec = FarmSpec(archetype=a, site=a.locations[0], year=2025, polygon_wgs84=box(5.6, 52.5, 5.601, 52.501),
                    soil=soil, elevation_m=0.0, region=a.region, wav_fraction=0.8, soil_evap_factor=0.75,
                    science_overrides={"RDMCR": 150.0})
    assert spec.wav_for(soil) == pytest.approx(0.8 * 0.17 * 150, abs=0.01)
    si = spec.science_inputs()
    assert si["WAV"] == spec.wav_for(soil) and si["overrides"] == {"RDMCR": 150.0} and si["soil_evap_factor"] == 0.75
    old = FarmSpec(archetype=a, site=a.locations[0], year=2025, polygon_wgs84=box(5.6, 52.5, 5.601, 52.501),
                   soil=soil, elevation_m=0.0, region=a.region)
    assert old.wav_for(soil) == 25.0                                   # unchanged without the layer
    del old.__dict__["science_overrides"]                              # a farm pickled before the layer existed
    assert old.science_inputs()["overrides"] == {}


def test_regional_variety_parameters_feed_the_stand_scaling():
    from fa_twin.hybrids import crop_table
    t = crop_table("maize", "US_CornBelt_2750GDU")
    assert t["TDWI"] == 21.2 and t["SPAN"] == 39.16 and "DTSMTB" in t
    ov = population_overrides("maize", t, 56000)
    assert ov["TDWI"] == pytest.approx(21.2 * 0.7, abs=1e-3)


@pytest.mark.parametrize("pop", [53000, 55500, 58000, 41111, 99999])
def test_stand_overrides_pass_pcse_partitioning_check(pop):
    """PCSE refuses a crop whose FL+FS+FO != 1 at emergence (it raised for 53 000/ha before)."""
    from fa_twin.engine import ZoneConfig, ZoneTwin
    from fa_twin.hybrids import crop_table
    ov = population_overrides("maize", crop_table("maize", "Grain_maize_204"), pop)
    recs = _records(dt.date(2004, 4, 1), 220)
    cfg = ZoneConfig("t", 41.0, -96.5, "maize", "Grain_maize_204", dt.date(2004, 5, 12), dt.date(2004, 10, 15),
                     param_overrides=ov)
    zt = ZoneTwin(cfg, records_provider(recs, 41.0, -96.5), engine="Wofost72_PP")
    zt.run_till(dt.date(2004, 7, 1))
    assert (zt.model.get_variable("TAGP") or 0) > 0


def test_observed_weather_accepts_real_continental_winters_but_not_wrong_units():
    """A Nebraska February day from the MEAD 6 S gauge (TMIN -12.2, TMAX -0.6) is real weather; the
    check exists to catch Kelvin or Fahrenheit, not cold places (records: -89.2 C to 56.7 C)."""
    from fa_twin.weather import validate_records
    recs = _records(days=5)
    cold = [dict(r) for r in recs]
    cold[2].update(TMIN=-12.2, TMAX=-0.6)
    cold[3].update(TMIN=-38.0, TMAX=-25.0)                      # a prairie cold snap
    assert validate_records(cold) == []
    kelvin = [dict(r, TMIN=r["TMIN"] + 273.15, TMAX=r["TMAX"] + 273.15) for r in recs]
    assert any("temperatures out of range" in p for p in validate_records(kelvin))
    swapped = [dict(r) for r in recs]
    swapped[1].update(TMIN=20.0, TMAX=10.0)
    assert any("temperatures out of range" in p for p in validate_records(swapped))


def test_variety_override_replaces_the_archetype_fidelity_note():
    """Building A4 with the calibrated Corn Belt hybrid must not keep warning about European parameters."""
    from fa_farm.archetypes import load_archetypes
    from fa_farm.build import apply_archetype_overrides
    a = load_archetypes()["A4"]
    assert "European" in a.fidelity_note
    b = apply_archetype_overrides(a, {"variety": "US_CornBelt_2750GDU", "sowing": "05-13"})
    assert b.variety == "US_CornBelt_2750GDU" and b.sowing == "05-13"
    assert "European" not in b.fidelity_note and "AmeriFlux" in b.fidelity_note
    assert apply_archetype_overrides(a, {"sowing": "05-02"}).fidelity_note == a.fidelity_note
