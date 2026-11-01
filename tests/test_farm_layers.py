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
    monkeypatch.setattr(SO, "_profile_wcs", lambda *a, **k: (_ for _ in ()).throw(DataUnavailable("soil", "x")))
    prof, sp, prov = SO.soilgrids_profile(37.88, 139.0, Cache(tmp_path))
    assert prof["clay"] == pytest.approx(0.30) and "nearest valid" in prov.note


def test_choose_field_accepts_unknown_confidence_when_none_rated():
    """FTW's US partition carries no confidence values: use the field, say so."""
    cands = json.loads((FIX / "ftw_nl_flevoland.json").read_text())
    for c in cands:
        c["confidence"] = None
    poly, attrs = B.choose_field(cands, lat=52.52, lon=5.62, min_ha=5, max_ha=20)
    assert attrs["confidence"] is None and attrs["confidence_unknown"] is True


def test_field_ndvi_aligns_20m_scl_with_10m_bands(tmp_path):
    """The 20 m scene-classification band must be resampled onto the 10 m grid,
    whatever the rounding of the windows (it used to drop every scene)."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import box as sbox
    crs = "EPSG:32615"
    x0, y0 = 500000.0, 4650000.0
    prof = dict(driver="GTiff", count=1, crs=crs, dtype="uint16")
    with rasterio.open(tmp_path / "red.tif", "w", width=200, height=200,
                       transform=from_origin(x0, y0, 10, 10), **prof) as d:
        d.write(np.full((1, 200, 200), 500, np.uint16))
    with rasterio.open(tmp_path / "nir.tif", "w", width=200, height=200,
                       transform=from_origin(x0, y0, 10, 10), **prof) as d:
        d.write(np.full((1, 200, 200), 3500, np.uint16))
    with rasterio.open(tmp_path / "scl.tif", "w", width=100, height=100,
                       transform=from_origin(x0, y0, 20, 20), **{**prof, "dtype": "uint8"}) as d:
        d.write(np.full((1, 100, 100), 4, np.uint8))

    class Asset:
        def __init__(self, h): self.href = str(h)

    class Item:
        properties = {"earthsearch:boa_offset_applied": True}
        assets = {"red": Asset(tmp_path / "red.tif"), "nir": Asset(tmp_path / "nir.tif"),
                  "scl": Asset(tmp_path / "scl.tif")}
    from pyproj import Transformer
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    # 41 rows at 10 m starting half a 20 m pixel down: the 20 m window rounds to
    # 20 rows (40 after doubling) -- the Iowa case that dropped every scene
    lo1, la1 = tr.transform(x0 + 105, y0 - 1015 - 410)
    lo2, la2 = tr.transform(x0 + 105 + 1610, y0 - 1015)
    nd = S._field_ndvi(Item(), sbox(min(lo1, lo2), min(la1, la2), max(lo1, lo2), max(la1, la2)))
    assert nd == pytest.approx((3500 - 500) / (3500 + 500), abs=1e-3)


def test_scl_read_onto_red_grid_odd_window(tmp_path):
    """Iowa case: 41 x 161 red window at an odd row offset; SCL must come back 41 x 161."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.windows import Window
    x0, y0, crs = 500000.0, 4650000.0, "EPSG:32615"
    prof = dict(driver="GTiff", count=1, crs=crs)
    with rasterio.open(tmp_path / "red.tif", "w", width=400, height=400, dtype="uint16",
                       transform=from_origin(x0, y0, 10, 10), **prof) as d:
        d.write(np.full((1, 400, 400), 500, np.uint16))
    scl = np.full((1, 200, 200), 4, np.uint8)
    scl[0, 50, :] = 9                                   # one cloudy 20 m row
    with rasterio.open(tmp_path / "scl.tif", "w", width=200, height=200, dtype="uint8",
                       transform=from_origin(x0, y0, 20, 20), **prof) as d:
        d.write(scl)
    with rasterio.open(tmp_path / "red.tif") as red, rasterio.open(tmp_path / "scl.tif") as sd:
        win = Window(col_off=11, row_off=101, width=161, height=41)
        out = S.scl_on_grid(sd, red, win)
    assert out.shape == (41, 161)
    assert (out == 9).sum() == 161 * 1 and out[0, 0] == 9     # row 101 at 10 m = row 50 at 20 m


def _wcs_grid(monkeypatch, masked_centre: bool):
    """Fake WCS: 9x9 cells of 250 m around the point; 0 = masked (water, paddy, urban)."""
    import numpy as np
    calls = []

    def fetch(prop, depth, x, y, half):
        calls.append((prop, depth))
        a = np.full((9, 9), {"sand": 300, "clay": 250, "soc": 120, "bdod": 140}[prop], np.int16)
        if masked_centre:
            a[1:8, 1:8] = 0
            a[4, 7] = {"sand": 500, "clay": 100, "soc": 80, "bdod": 150}[prop]   # nearest valid: 750 m east
        return a, (x - half, y + half, 250.0)
    monkeypatch.setattr(SO, "_wcs_window", fetch)
    return calls


def test_soilgrids_wcs_reads_one_small_window_per_layer(tmp_path, monkeypatch):
    calls = _wcs_grid(monkeypatch, masked_centre=False)
    monkeypatch.setattr(SO, "_sample_vrt", lambda *a: pytest.fail("the 6 MB VRTs must not be opened"))
    seen = []
    prof, sp, prov = SO.soilgrids_profile(28.47, 77.54, Cache(tmp_path),
                                          progress=lambda done, total: seen.append((done, total)))
    assert len(calls) == 16 and prof["sand"] == pytest.approx(0.30) and prof["bdod"] == pytest.approx(1.40)
    assert seen[-1] == (16, 16) and "nearest valid" not in prov.note


def test_soilgrids_wcs_takes_nearest_valid_cell(tmp_path, monkeypatch):
    _wcs_grid(monkeypatch, masked_centre=True)
    prof, sp, prov = SO.soilgrids_profile(28.47, 77.54, Cache(tmp_path))
    assert prof["sand"] == pytest.approx(0.50) and "nearest valid cell 750 m" in prov.note


@pytest.mark.network
def test_soilgrids_wcs_matches_vrt(tmp_path):
    lat, lon = 52.5, 5.6
    wcs = SO._profile_wcs(lat, lon)
    vrt = SO._profile_at(lat, lon)
    for k in ("sand", "clay", "soc_pct", "bdod"):
        assert wcs[k] == pytest.approx(vrt[k], rel=0.02, abs=1e-3)
