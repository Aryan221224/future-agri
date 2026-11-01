"""Drone twin API: the spec sheet for the 3D model, mission simulation with a track."""
import base64

import numpy as np
from fastapi.testclient import TestClient

from test_console_farms import _create, app, fake_builder  # noqa: F401  (fixture)

CALM = {"wind_ms": 1.5, "wind_dir_deg": 270, "gust_ms": 2.5, "temp_c": 20}


def test_spec_payload_matches_sheet(app):
    with TestClient(app) as c:
        s = c.get("/api/drone/spec").json()
        assert s["wheelbase_mm"] == 1300 and s["rotor_diameter_m"] == 0.8814 and s["tank_l"] == 10
        assert s["unfolded_mm"] == [920, 920, 480] and s["n_rotors"] == 4
        assert len(s["nozzles"]) == 4 and all(abs(abs(n["x"]) - 0.4596) < 1e-3 for n in s["nozzles"])
        assert s["battery"]["inferred"] is True and s["limits"]["wind_ms"] == 5.4
        assert s["controller"] == "JIYI K3-A Pro"


def test_mission_endpoint_returns_track(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        r = c.post(f"/api/farms/{f['id']}/mission", json={"settings": {"speed_ms": 5.0}, "weather": CALM})
        assert r.status_code == 200, r.text
        m = r.json()
        assert m["feasible"] is True and m["track"][-1]["phase"] == "landed"
        assert {"time_min", "sorties", "litres", "cv_pct", "ha_per_hour"} <= set(m["kpis"])
        assert m["route"] and set(m["route"][0]) == {"start", "end"}
        dose = np.frombuffer(base64.b64decode(m["dose_b64"]), "<f2")
        assert len(dose) == 100 and dose.max() > 0
        assert len(m["home"]) == 2 and m["settings"]["speed_ms"] == 5.0


def test_mission_rejects_bad_settings(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        r = c.post(f"/api/farms/{f['id']}/mission", json={"settings": {"speed_ms": 0}, "weather": CALM})
        assert r.status_code == 422
        r = c.post(f"/api/farms/{f['id']}/mission", json={"targets": [0, 5000], "weather": CALM})
        assert r.status_code == 422 and "cell" in r.text


def test_mission_wind_too_high_is_infeasible(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        r = c.post(f"/api/farms/{f['id']}/mission",
                   json={"weather": {"wind_ms": 9, "wind_dir_deg": 0, "gust_ms": 12, "temp_c": 20}})
        assert r.status_code == 200
        m = r.json()
        assert m["feasible"] is False and any("wind" in x for x in m["reasons"]) and m["track"] == []


def test_spot_spray_targets_subset(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        r = c.post(f"/api/farms/{f['id']}/mission", json={"targets": list(range(0, 30)), "weather": CALM})
        m = r.json()
        assert m["feasible"] and m["kpis"]["area_ha"] < 0.5
