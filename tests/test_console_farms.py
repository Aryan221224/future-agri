"""Console farms: create (sim | field), build job, 3D scene payload."""
import base64
import dataclasses

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fa_console.app import create_app
from fa_console.config import Settings
from fa_farm.sources.base import DataUnavailable
from test_runner import _vf

LAT, LON = 52.52, 5.62


def _square(side_m, lat=LAT, lon=LON):
    dlat = side_m / 111_320
    dlon = side_m / (111_320 * np.cos(np.radians(lat)))
    ring = [[lon, lat], [lon + dlon, lat], [lon + dlon, lat + dlat], [lon, lat + dlat], [lon, lat]]
    return {"type": "Polygon", "coordinates": [ring]}


def fake_builder(template, year, cache, site_index=0, cell_m=10.0, seed=0, polygon_wgs84=None,
                 progress=None, **kw):
    vf = _vf()
    if progress:
        progress(0.5, "weather")
    if polygon_wgs84 is not None:
        vf.spec = dataclasses.replace(vf.spec, polygon_wgs84=polygon_wgs84, cell_m=cell_m)
    return vf


@pytest.fixture
def app(tmp_path):
    a = create_app(Settings(home=tmp_path / "home", cache=tmp_path / "cache", offline=True),
                   start_workers=False)
    a.state.farms.builder = fake_builder
    return a


def _create(c, app, **over):
    body = {"name": "Test field", "mode": "sim", "template": "A5", "polygon": _square(100), "year": 2025}
    body.update(over)
    r = c.post("/api/farms", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    app.state.jobs.run_pending()
    return out["farm"], out["job"]


def test_create_sim_farm_and_build_job(app):
    with TestClient(app) as c:
        f, j = _create(c, app)
        assert c.get(f"/api/jobs/{j['id']}").json()["status"] == "done"
        g = c.get(f"/api/farms/{f['id']}").json()
        assert g["status"] == "ready" and g["area_ha"] == pytest.approx(1.0, rel=0.15)
        assert g["crop"] == "potato" and g["region"] == "EU" and g["sowing"]
        assert (app.state.store.farm_dir(f["id"]) / "farm.pkl").exists()
        assert [x["id"] for x in c.get("/api/farms").json()["farms"]] == [f["id"]]


def test_build_failure_names_layer(app):
    def broken(*a, **k):
        raise DataUnavailable("field boundaries", "no FTW field near the point")
    app.state.farms.builder = broken
    with TestClient(app) as c:
        f, j = _create(c, app)
        job = c.get(f"/api/jobs/{j['id']}").json()
        assert job["status"] == "failed" and "field boundaries" in job["error"]
        g = c.get(f"/api/farms/{f['id']}").json()
        assert g["status"] == "error" and "field boundaries" in g["meta"]["error"]


def test_uploaded_polygon_skips_boundary_detection(app, monkeypatch):
    import fa_console.farms as farms

    def no_ftw(*a, **k):
        raise AssertionError("boundary detection must not run when a polygon is uploaded")
    monkeypatch.setattr(farms, "field_near", no_ftw)
    with TestClient(app) as c:
        f, j = _create(c, app)
        assert c.get(f"/api/jobs/{j['id']}").json()["status"] == "done"


def test_point_without_country_rejected(app):
    with TestClient(app) as c:
        r = c.post("/api/farms", json={"name": "x", "mode": "sim", "template": "A5", "lat": 10.0, "lon": 20.0})
        assert r.status_code == 422 and "country" in r.text


def test_area_limit_and_cell_cap(app):
    with TestClient(app) as c:
        r = c.post("/api/farms", json={"name": "Huge", "mode": "sim", "template": "A5",
                                       "polygon": _square(2500), "year": 2025})
        assert r.status_code == 422 and "500 ha" in r.text
        f, _ = _create(c, app, polygon=_square(1732), cell_m=5.0)          # ~300 ha
        g = c.get(f"/api/farms/{f['id']}").json()
        assert g["cell_m"] > 5.0
        assert g["area_ha"] * 1e4 / g["cell_m"] ** 2 <= 20_000 * 1.05
        assert any("cell size" in w for w in g["meta"]["warnings"])


def test_scene_payload_shapes(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        s = c.get(f"/api/farms/{f['id']}/scene").json()
        n = s["cells"]["n"]
        x = np.frombuffer(base64.b64decode(s["cells"]["x_b64"]), "<f4")
        y = np.frombuffer(base64.b64decode(s["cells"]["y_b64"]), "<f4")
        assert n == len(x) == len(y) == 100
        t = s["terrain"]
        h = np.frombuffer(base64.b64decode(t["heights_b64"]), "<f4")
        assert len(h) == t["nx"] * t["ny"] and np.allclose(h, 0)
        assert any("terrain" in w for w in s["warnings"])          # offline, nothing cached: flat
        assert len(s["polygon"]) >= 4 and s["imagery"]["available"] is False
        zone = np.frombuffer(base64.b64decode(s["layers"]["zone_b64"]), "<f2")
        assert len(zone) == n


def test_delete_farm_removes_files(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        d = app.state.store.farm_dir(f["id"])
        assert c.delete(f"/api/farms/{f['id']}").status_code == 200
        assert not d.exists() and c.get(f"/api/farms/{f['id']}").status_code == 404


def test_field_mode_requires_sowing(app):
    with TestClient(app) as c:
        r = c.post("/api/farms", json={"name": "Live", "mode": "field", "template": "A5", "polygon": _square(100)})
        assert r.status_code == 422 and "sowing" in r.text


@pytest.mark.network
def test_build_real_template_farm_A6(tmp_path):
    a = create_app(Settings(home=tmp_path / "home"), start_workers=False)
    with TestClient(a) as c:
        r = c.post("/api/farms", json={"name": "Kenya plot", "mode": "sim", "template": "A6", "cell_m": 5.0})
        assert r.status_code == 200, r.text
        a.state.jobs.run_pending()
        f = c.get(f"/api/farms/{r.json()['farm']['id']}").json()
        assert f["status"] == "ready", f["meta"]
        s = c.get(f"/api/farms/{f['id']}/scene").json()
        assert s["terrain"]["max"] > s["terrain"]["min"]
