"""Task 3: soil, terrain, field boundaries, Sentinel-2 cover."""
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pytest

from fa_farm.sources import boundaries as B
from fa_farm.sources import satellite as S
from fa_farm.sources import soil as SO
from fa_farm.sources import terrain as T
from fa_farm.sources.base import Cache

FIX = Path(__file__).parent / "fixtures" / "farm"


def test_saxton_rawls_loam():
    p = SO.saxton_rawls(sand=0.40, clay=0.20, om_pct=2.5)
    assert abs(p["SMFCF"] - 0.27) <= 0.02
    assert abs(p["SMW"] - 0.13) <= 0.02
    assert abs(p["SM0"] - 0.44) <= 0.03
    assert p["SMW"] < p["SMFCF"] < p["SM0"]
    assert 1.0 < p["KSAT_mm_h"] < 60.0


def test_saxton_rawls_sand_drains_faster_than_clay():
    sand = SO.saxton_rawls(0.85, 0.05, 1.0)
    clay = SO.saxton_rawls(0.15, 0.55, 2.0)
    assert sand["KSAT_mm_h"] > 5 * clay["KSAT_mm_h"]
    assert sand["SMFCF"] - sand["SMW"] < clay["SM0"]


def test_soil_params_from_profile():
    sp = SO.to_soil_params({"sand": 0.30, "clay": 0.30, "soc_pct": 1.5}, rdmsol_cm=120)
    assert sp.SMW < sp.SMFCF < sp.SM0 and sp.RDMSOL == 120 and sp.K0 > 0


def test_twi_higher_in_valley():
    x = np.abs(np.arange(-20, 21))[None, :].repeat(41, axis=0).astype(float)   # V valley along y
    dem = x * 0.5 + np.arange(41)[:, None] * 0.05                              # gentle slope down to row 0
    twi = T.twi(dem, res_m=30.0)
    assert twi[20, 20] > twi[20, 2]          # valley floor wetter than the ridge
    s = T.slope_deg(dem, 30.0)
    assert s.shape == dem.shape and np.nanmax(s) < 5


def test_choose_field_from_real_ftw_fixture():
    cands = json.loads((FIX / "ftw_nl_flevoland.json").read_text())
    poly, attrs = B.choose_field(cands, lat=52.52, lon=5.62, min_ha=5, max_ha=20, min_conf=69)
    from fa_core.geo import area_ha
    assert 5 <= area_ha(poly) <= 20.5
    assert attrs["confidence"] >= 69 and attrs["id"]


def test_choose_field_none_in_range_raises():
    cands = json.loads((FIX / "ftw_nl_flevoland.json").read_text())
    with pytest.raises(B.NoFieldFound):
        B.choose_field(cands, lat=52.52, lon=5.62, min_ha=500, max_ha=900)


def test_fvc_bounds():
    assert S.fvc(0.1) == 0.0
    assert S.fvc(0.95) == 1.0
    assert 0.2 < S.fvc(0.55) < 0.4


def test_dem_tile_name():
    assert T.tile_name(52.5, 5.6) == "Copernicus_DSM_COG_10_N52_00_E005_00_DEM"
    assert T.tile_name(-12.5, -55.7) == "Copernicus_DSM_COG_10_S13_00_W056_00_DEM"


@pytest.mark.network
def test_soilgrids_profile_network(tmp_path):
    prof, sp, prov = SO.soilgrids_profile(52.5, 5.6, Cache(tmp_path))
    assert 0 < prof["clay"] < 1 and prov.licence == "CC-BY-4.0"


@pytest.mark.network
def test_dem_window_network(tmp_path):
    dem, res, prov = T.dem_window(52.5, 5.6, 300, Cache(tmp_path))
    assert dem.shape[0] > 10 and -20 < np.nanmean(dem) < 20


@pytest.mark.network
def test_field_near_network(tmp_path):
    poly, attrs, prov = B.field_near(52.52, 5.62, "NL", Cache(tmp_path), 5, 20)
    assert prov.licence == "CC-BY-4.0"


@pytest.mark.network
def test_s2_series_network(tmp_path):
    cands = json.loads((FIX / "ftw_nl_flevoland.json").read_text())
    poly, _ = B.choose_field(cands, 52.52, 5.62, 5, 20)
    ser, prov = S.s2_cover_series(poly, dt.date(2025, 5, 1), dt.date(2025, 6, 15), Cache(tmp_path))
    assert len(ser) >= 2 and all(0 <= f <= 1 for _, _, f in ser)


def test_soilgrids_falls_back_to_nearest_valid_cell(tmp_path, monkeypatch):
    """Paddy and water-masked cells have no SoilGrids value: search outward."""
    from fa_farm.sources.base import DataUnavailable

    def fake(url, lat, lon):
        if abs(lat - 37.88) < 1e-6 and abs(lon - 139.0) < 1e-6:
            raise DataUnavailable("soil", "no SoilGrids value")
        return {"sand": 300.0, "clay": 300.0, "soc": 150.0, "bdod": 130.0}[url.split("/")[-2]]
    monkeypatch.setattr(SO, "_sample_vrt", fake)
    prof, sp, prov = SO.soilgrids_profile(37.88, 139.0, Cache(tmp_path))
    assert prof["clay"] == pytest.approx(0.30) and "nearest valid" in prov.note


def test_choose_field_accepts_unknown_confidence_when_none_rated():
    """FTW's US partition carries no confidence values: use the field, say so."""
    cands = json.loads((FIX / "ftw_nl_flevoland.json").read_text())
    for c in cands:
        c["confidence"] = None
    poly, attrs = B.choose_field(cands, lat=52.52, lon=5.62, min_ha=5, max_ha=20)
    assert attrs["confidence"] is None and attrs["confidence_unknown"] is True
