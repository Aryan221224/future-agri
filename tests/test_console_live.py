"""Live feeds (weather, forecast, spray outlook, satellite, alerts) and field devices."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

import fa_console.live as live
from fa_farm.sources.base import DataUnavailable
from test_console_farms import _create, app, fake_builder  # noqa: F401  (fixture)
from test_runner import _vf


def _members(n=10, days=10, wind=1.0, rain=0.0):
    start = dt.date(2025, 5, 1)
    return [[{"DAY": start + dt.timedelta(days=d), "TMAX": 20.0, "TMIN": 10.0, "RAIN": rain,
              "IRRAD": 1.5e7, "VAP": 12.0, "WIND": wind + 0.4 * (m % 5) * (d % 3)}
             for d in range(days)] for m in range(n)]


def test_weather_served_stale_when_offline(app, monkeypatch):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        good = {"observed": [{"day": "2025-05-01", "tmax": 20}], "forecast": None}
        monkeypatch.setattr(live.LiveService, "_fetch_weather", lambda self, farm, vf: good)
        r = c.get(f"/api/farms/{fid}/live/weather").json()
        assert r["available"] and r["stale"] is False and r["observed"][0]["tmax"] == 20

        def boom(self, farm, vf):
            raise DataUnavailable("weather", "no network")
        monkeypatch.setattr(live.LiveService, "_fetch_weather", boom)
        app.state.live.ttl["weather"] = 0                     # force a refetch
        r = c.get(f"/api/farms/{fid}/live/weather")
        assert r.status_code == 200
        r = r.json()
        assert r["available"] and r["stale"] is True and "no network" in r["stale_reason"]


def test_weather_unavailable_without_cache(app, monkeypatch):
    def boom(self, farm, vf):
        raise DataUnavailable("weather", "no network")
    monkeypatch.setattr(live.LiveService, "_fetch_weather", boom)
    with TestClient(app) as c:
        f, _ = _create(c, app)
        r = c.get(f"/api/farms/{f['id']}/live/weather")
        assert r.status_code == 200
        assert r.json()["available"] is False and "no network" in r.json()["reason"]


def test_outlook_probability_bounds(app, monkeypatch):
    monkeypatch.setattr(live.LiveService, "_forecast_members",
                        lambda self, farm, vf: (_members(), "ecmwf-ifs-ens", "2025-05-01"))
    with TestClient(app) as c:
        f, _ = _create(c, app)
        o = c.get(f"/api/farms/{f['id']}/live/outlook").json()
        assert o["available"] and len(o["days"]) == 10 and o["source"] == "ecmwf-ifs-ens"
        assert all(0.0 <= d["p_flyable"] <= 1.0 for d in o["days"])
        assert o["days"][0]["p_flyable"] == 1.0                # calm first day
        assert o["limits"]["wind_ms"] == 3.0                  # EU label limit is stricter than the aircraft
        monkeypatch.setattr(live.LiveService, "_forecast_members",
                            lambda self, farm, vf: (_members(rain=5.0), "ecmwf-ifs-ens", "2025-05-01"))
        app.state.live.ttl["outlook"] = 0
        o = c.get(f"/api/farms/{f['id']}/live/outlook").json()
        assert all(d["p_flyable"] == 0.0 for d in o["days"])


def test_satellite_series_from_farm(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        s = c.get(f"/api/farms/{f['id']}/live/satellite").json()
        assert s["available"] and s["series"] == [] and s["latest"]["available"] is False


def test_alerts_include_waiting_proposals(app):
    app.state.sessions.defaults["plan_members"] = 4
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        j = c.post(f"/api/farms/{fid}/session/run", json={"stop_on_decision": True}).json()
        app.state.jobs.run_pending()
        a = c.get(f"/api/farms/{fid}/live/alerts").json()["alerts"]
        codes = {x["code"] for x in a}
        assert "proposal_waiting" in codes and "region_derogation" in codes
        assert all(x["level"] in ("info", "warn", "alert") and x["text"] for x in a)


def test_device_ingest_auth_and_validation(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        r = c.post(f"/api/farms/{fid}/devices", json={"kind": "soil_moisture", "name": "Probe 1"})
        assert r.status_code == 200
        tok = r.json()["token"]
        assert "token" not in c.get(f"/api/farms/{fid}/devices").json()["devices"][0]
        bad = c.post("/api/ingest", json={"token": "wrong", "readings": []})
        assert bad.status_code == 401
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        r = c.post("/api/ingest", json={"token": tok, "readings": [
            {"kind": "soil_moisture", "ts": now, "value": 23.5, "unit": "%"},
            {"kind": "soil_moisture", "ts": "yesterday", "value": 1},
            {"kind": "soil_moisture", "ts": now, "value": 250, "unit": "%"}]})
        assert r.status_code == 200
        out = r.json()
        assert out["accepted"] == 1 and len(out["rejected"]) == 2
        assert {x["index"] for x in out["rejected"]} == {1, 2}
        rd = c.get(f"/api/farms/{fid}/readings", params={"kind": "soil_moisture"}).json()["readings"]
        assert len(rd) == 1 and rd[0]["value"] == 23.5
        dev = c.get(f"/api/farms/{fid}/devices").json()["devices"][0]
        assert dev["last_seen"]


def test_tracker_readings_appear_as_positions(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        tok = c.post(f"/api/farms/{fid}/devices", json={"kind": "tracker", "name": "K3A tracker"}).json()["token"]
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        r = c.post("/api/ingest", json={"token": tok, "readings": [
            {"kind": "position", "ts": now, "payload": {"lat": 52.5204, "lon": 5.6204, "alt_m": 2.5,
                                                         "heading_deg": 90, "speed_ms": 5.0, "spraying": True}},
            {"kind": "position", "ts": now, "payload": {"lat": 95.0, "lon": 5.62}}]})
        assert r.json()["accepted"] == 1
        t = c.get(f"/api/farms/{fid}/tracker").json()
        assert len(t["positions"]) == 1
        p = t["positions"][0]
        assert -20 < p["x"] - t["origin_x"] < 120 and p["z"] == 2.5 and p["spraying"] is True
