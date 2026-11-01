"""Live data sources: recent NASA POWER days, the newest forecast issue, Sentinel-2
true colour, and importing an uploaded camera-drone scan."""
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import box

from fa_farm.grid import FarmGrid
from fa_farm.sources import forecasts as F
from fa_farm.sources import weather as W
from fa_farm.sources.base import Cache, DataUnavailable
from fa_scan.importer import import_scan

FIX = Path(__file__).parent / "fixtures" / "farm"


def _raw_with_trailing_fill(n_missing: int):
    raw = json.loads((FIX / "power_nl_202504.json").read_text())
    par = raw["properties"]["parameter"]
    keys = sorted(par["T2M_MAX"])
    for k in keys[len(keys) - n_missing:]:
        for name in par:
            par[name][k] = -999.0
    return raw, len(keys)


def test_power_recent_drops_trailing_missing_days(monkeypatch, tmp_path):
    raw, n = _raw_with_trailing_fill(3)
    monkeypatch.setattr(W, "fetch_json", lambda *a, **k: raw)
    recs, prov, elev = W.power_recent(52.5, 5.6, dt.date(2000, 1, 1), Cache(tmp_path),
                                      today=dt.date(2030, 1, 1))
    assert len(recs) == n - 3
    assert all(r["TMAX"] is not None and r["WIND"] is not None for r in recs)
    assert "lag" in prov.note


def test_power_recent_all_missing_raises(monkeypatch, tmp_path):
    raw, n = _raw_with_trailing_fill(10_000)
    monkeypatch.setattr(W, "fetch_json", lambda *a, **k: raw)
    with pytest.raises(DataUnavailable):
        W.power_recent(52.5, 5.6, dt.date(2000, 1, 1), Cache(tmp_path), today=dt.date(2030, 1, 1))


class _FakeDS:
    def __init__(self, stamps):
        self.init_time = type("Coord", (), {"values": np.array(stamps, dtype="datetime64[ns]")})()


def test_latest_issue_reads_max_init_time(monkeypatch):
    monkeypatch.setattr(F, "_dataset", lambda src: _FakeDS(["2026-10-29T00", "2026-10-31T00",
                                                             "2026-10-30T00"]))
    F.latest_issue.cache_clear()
    assert F.latest_issue(F.ForecastSource.IFS_ENS) == dt.date(2026, 10, 31)
    F.latest_issue.cache_clear()


def _grid():
    lat, lon = 52.52, 5.62
    dlat, dlon = 100 / 111_320, 100 / (111_320 * np.cos(np.radians(lat)))
    return FarmGrid.from_wgs84(box(lon, lat, lon + dlon, lat + dlat), 10.0), (lon, lat, dlon, dlat)


def _pt(lon, lat, cls, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"class": cls, **props}}


def test_import_scan_counts_outside_and_bad_features():
    g, (lon, lat, dlon, dlat) = _grid()
    feats = [_pt(lon + 0.55 * dlon, lat + 0.55 * dlat, "weed", confidence=0.8),
             _pt(lon + 0.25 * dlon, lat + 0.75 * dlat, "disease", severity=0.02),
             _pt(lon - dlon, lat, "weed"),                                   # outside the field
             {"type": "Feature", "geometry": {"type": "LineString",
                                              "coordinates": [[lon, lat], [lon + dlon, lat]]},
              "properties": {"class": "weed"}},                              # not a point
             _pt(lon + 0.5 * dlon, lat + 0.5 * dlat, "tractor")]             # unknown class
    obs, rep = import_scan({"type": "FeatureCollection", "features": feats}, g, 0.005, day_index=30)
    assert rep["points"] == 2 and rep["weeds"] == 1 and rep["disease"] == 1
    assert rep["outside"] == 1 and rep["bad"] == 2 and len(rep["errors"]) == 2
    assert int(obs.blight_flag.sum()) == 1
    assert len(obs.cc_cells) == g.n_cells == 100
    assert obs.weed_detections.sum() == 1


def test_import_scan_densities_are_recall_corrected():
    g, (lon, lat, dlon, dlat) = _grid()
    feats = [_pt(lon + 0.05 * dlon, lat + 0.05 * dlat, "weed") for _ in range(20)]
    obs, _ = import_scan({"type": "FeatureCollection", "features": feats}, g, gsd_m=0.02, day_index=10,
                         fp_per_m2=0.0)
    i = int(np.argmax(obs.weed_detections))
    assert obs.weed_detections[i] == 20
    assert 0 < obs.weed_recall_used < 1
    assert obs.weed_density_est[i] == pytest.approx(20 / obs.weed_recall_used / g.cell_m ** 2)
    assert obs.weed_density_est[i] > 20 / g.cell_m ** 2


def test_import_scan_without_cover_is_uninformative():
    g, _ = _grid()
    obs, rep = import_scan({"type": "FeatureCollection", "features": []}, g, 0.005, day_index=30)
    assert obs.cc_sd >= 1.0 and any("cover" in a for a in obs.assumptions)


@pytest.mark.network
def test_s2_rgb_chip_flevoland(tmp_path):
    from fa_farm.sources.satellite import encode_png, s2_rgb_chip
    from fa_farm.sources.boundaries import choose_field
    cands = json.loads((FIX / "ftw_nl_flevoland.json").read_text())
    poly, _ = choose_field(cands, 52.52, 5.62, 5.0, 40.0)
    rgb, meta, prov = s2_rgb_chip(poly, dt.date(2025, 6, 1), dt.date(2025, 7, 31), Cache(tmp_path))
    assert rgb.ndim == 3 and rgb.shape[2] == 3 and rgb.dtype == np.uint8
    assert dt.date(2025, 6, 1) <= dt.date.fromisoformat(meta["date"]) <= dt.date(2025, 7, 31)
    assert encode_png(rgb)[:8] == b"\x89PNG\r\n\x1a\n"
