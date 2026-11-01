"""Mission sessions over HTTP: the closed loop, step by step, for virtual and live farms."""
import dataclasses
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from fa_console.app import create_app
from fa_farm.archetypes import season_dates
from fa_farm.build import FieldFarm
from test_console_farms import _create, _square, app, fake_builder  # noqa: F401  (fixture)
from test_runner import _vf


@pytest.fixture(autouse=True)
def quick(app):
    app.state.sessions.defaults["plan_members"] = 4


def _state(c, fid):
    r = c.get(f"/api/farms/{fid}/session")
    assert r.status_code == 200, r.text
    return r.json()


def _run(c, app, fid, **body):
    j = c.post(f"/api/farms/{fid}/session/run", json=body)
    assert j.status_code == 200, j.text
    app.state.jobs.run_pending()
    job = c.get(f"/api/jobs/{j.json()['id']}").json()
    assert job["status"] == "done", job
    return _state(c, fid)


def test_step_advances_one_day(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        s0 = _state(c, f["id"])
        assert s0["phase"] == "morning" and s0["approvals"] == "manual" and s0["policy"] == "twin"
        r = c.post(f"/api/farms/{f['id']}/session/step")
        assert r.status_code == 200, r.text
        s1 = r.json()["state"]
        assert dt.date.fromisoformat(s1["day"]) == dt.date.fromisoformat(s0["day"]) + dt.timedelta(days=1)


def test_run_stops_on_decision_and_approve_flies(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        s = _run(c, app, fid, stop_on_decision=True)
        props = [o for o in s["ops"] if o["status"] == "proposed"]
        assert props and s["phase"] == "flights"
        op = props[0]
        r = c.post(f"/api/farms/{fid}/session/approve", json={"op_id": op["id"]})
        assert r.status_code == 200, r.text
        until = (dt.date.fromisoformat(s["day"]) + dt.timedelta(days=12)).isoformat()
        s = _run(c, app, fid, until=until, stop_on_decision=False)
        status = {o["id"]: o["status"] for o in s["ops"]}
        assert status[op["id"]] in ("flown", "dropped")
        if status[op["id"]] == "flown":
            tr = c.get(f"/api/farms/{fid}/session/tracks/{op['id']}").json()
            assert tr["track"][-1]["phase"] == "landed"
        assert {o["id"] for o in app.state.store.list_operations(fid)} == set(status)
        t = c.get(f"/api/farms/{fid}/session/trace").json()
        assert len(t["trace"]) >= 30 and t["events"]
        snaps = c.get(f"/api/farms/{fid}/session/snapshots").json()
        assert snaps["days"] and set(snaps["data"][snaps["days"][0]]) >= {"lai_b64", "blight_b64", "weeds_b64"}


def test_bad_action_is_422_not_500(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        r = c.post(f"/api/farms/{f['id']}/session/approve", json={"op_id": "nope"})
        assert r.status_code == 422 and "unknown operation" in r.text


def test_restart_replays_to_same_state(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        s = _run(c, app, fid, stop_on_decision=True)
        op = next(o for o in s["ops"] if o["status"] == "proposed")
        assert c.post(f"/api/farms/{fid}/session/defer", json={"op_id": op["id"], "days": 2}).status_code == 200
        assert c.post(f"/api/farms/{fid}/session/step").status_code == 200
        before = _state(c, fid)
    app2 = create_app(app.state.settings, start_workers=False)       # a server restart
    app2.state.farms.builder = fake_builder
    app2.state.sessions.defaults["plan_members"] = 4
    with TestClient(app2) as c2:
        after = _state(c2, fid)
    for k in ("day", "phase", "ops", "costs", "missions", "notes"):
        assert after[k] == before[k], k


def test_concurrent_action_gets_409(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        _state(c, fid)
        lock = app.state.sessions._lock(fid)
        lock.acquire()
        try:
            r = c.post(f"/api/farms/{fid}/session/step")
            assert r.status_code == 409 and "busy" in r.text
            day = _state(c, fid)["day"]                      # reads still work while busy
        finally:
            lock.release()
        r = c.post(f"/api/farms/{fid}/session/step")
        assert r.status_code == 200 and r.json()["state"]["day"] > day


def _fake_field(days_observed):
    def build(template, poly, sowing, cache, cell_m=10.0, region=None, seed=0, country="", progress=None, **kw):
        vf = _vf()
        spec = dataclasses.replace(vf.spec, polygon_wgs84=poly, cell_m=cell_m)
        sow, harv = season_dates(spec.archetype, spec.year)
        obs = [r for r in vf.season_weather if r["DAY"] <= sow + dt.timedelta(days=days_observed)]
        return FieldFarm(spec, obs, vf.history, [], harv, [], [])
    return build


def _create_field(c, app, days_observed=50):
    app.state.farms.field_builder = _fake_field(days_observed)
    app.state.farms.today = lambda: dt.date(2025, 6, 15)
    a = app.state.farms.template("A5")
    sow, _ = season_dates(a, 2025)
    r = c.post("/api/farms", json={"name": "Live field", "mode": "field", "template": "A5",
                                   "polygon": _square(100), "sowing": sow.isoformat()})
    assert r.status_code == 200, r.text
    app.state.jobs.run_pending()
    return r.json()["farm"], sow


def test_field_session_waits_for_weather_and_scan(app):
    with TestClient(app) as c:
        f, sow = _create_field(c, app, days_observed=50)
        fid = f["id"]
        s = _run(c, app, fid, stop_on_decision=False, approve_all=False)
        assert dt.date.fromisoformat(s["day"]) == sow + dt.timedelta(days=52)   # next morning to process
        assert s["world"]["has_truth"] is False and s["world"]["waiting"]
        scan_ops = [o for o in s["ops"] if o["product"] == "scan"]
        assert scan_ops and scan_ops[0]["status"] == "proposed"
        lon, lat = 5.6205, 52.5205
        gj = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
             "properties": {"class": "weed"}}]}
        r = c.post(f"/api/farms/{fid}/session/upload-scan", json={"geojson": gj, "gsd_m": 0.005})
        assert r.status_code == 200, r.text
        assert r.json()["report"]["weeds"] == 1
        s = _state(c, fid)
        assert [o["status"] for o in s["ops"] if o["product"] == "scan"][0] == "flown"
        open_sprays = [o for o in s["ops"] if o["product"] != "scan" and o["status"] in ("proposed", "approved")]
        if open_sprays:
            op = open_sprays[0]
            r = c.post(f"/api/farms/{fid}/session/fly", json={"op_id": op["id"]})
            assert r.status_code == 422 and "record" in r.text
            r = c.post(f"/api/farms/{fid}/session/record", json={"op_id": op["id"], "record": {
                "day": s["day"], "litres": 12.0, "wind_ms": 1.2, "temp_c": 18.0}})
            assert r.status_code == 200, r.text
            done = {o["id"]: o for o in r.json()["state"]["ops"]}[op["id"]]
            assert done["status"] == "flown" and done["result"]["source"] == "record"


def test_finish_scores_only_in_sim(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        s = _run(c, app, f["id"], stop_on_decision=False, approve_all=True)
        assert s["done"]
        r = c.post(f"/api/farms/{f['id']}/session/finish")
        assert r.status_code == 200 and r.json()["net_ha"] is not None
        g, _ = _create_field(c, app, days_observed=20)
        r = c.post(f"/api/farms/{g['id']}/session/finish")
        assert r.status_code == 422


def test_field_replay_ignores_weather_that_arrived_later(app):
    import json as _json
    with TestClient(app) as c:
        f, sow = _create_field(c, app, days_observed=50)
        fid = f["id"]
        before = _run(c, app, fid, stop_on_decision=False)
        assert before["world"]["waiting"]
    # ten more days are published after the run: a refresh would write them here
    later = [r for r in _vf().season_weather
             if sow + dt.timedelta(days=50) < r["DAY"] <= sow + dt.timedelta(days=60)]
    (app.state.store.farm_dir(fid) / "weather_extra.json").write_text(
        _json.dumps([dict(r, DAY=r["DAY"].isoformat()) for r in later]))
    app2 = create_app(app.state.settings, start_workers=False)
    app2.state.farms.builder = fake_builder
    app2.state.sessions.defaults["plan_members"] = 4
    with TestClient(app2) as c2:
        after = _state(c2, fid)
        assert after["day"] == before["day"] and after["ops"] == before["ops"]
        assert after["world"]["last_observed"] > before["world"]["last_observed"]   # new data is there to use
        assert after["world"]["waiting"] is None
