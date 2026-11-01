"""Live run over HTTP: a closed-loop job flown with the simulated sprayer camera, or fed by
the aircraft's own messages (idempotent), re-planned, frozen, finished."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from fa_core.live_msgs import SCHEMA
from fa_runner.scenarios import flight_record
from test_console_farms import _create, app, fake_builder  # noqa: F401  (fixture)


def _weedy_builder(*a, **k):
    vf = fake_builder(*a, **k)
    vf.spec = dataclasses.replace(vf.spec, weed_mean_m2=25.0)
    return vf


@pytest.fixture
def herb(app):
    """(client, farm id, herbicide op id) on a flyable day of a virtual farm."""
    app.state.farms.builder = _weedy_builder
    app.state.sessions.defaults["plan_members"] = 4
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        svc = app.state.sessions
        op = None
        for _ in range(200):
            svc.act(fid, "run_until", {"stop_on_decision": True})
            s = svc.get(fid)
            props = [o for o in s.ops.values() if o.status == "proposed"]
            op = next((o for o in props if o.product == "herbicide"), None)
            if op:
                break
            for o in props:
                svc.act(fid, "approve", {"op_id": o.id})
        assert op is not None
        for _ in range(40):
            rec = s.world.weather(s.day)
            if rec is not None and s.window.ok(flight_record(rec)):
                break
            svc.act(fid, "step", {})
        yield c, fid, op.id


def _wait(app, c, jid):
    app.state.jobs.run_pending()
    j = c.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "done", j
    return j


def test_virtual_live_run_publishes_each_sortie(app, herb):
    c, fid, op = herb
    r = c.post(f"/api/farms/{fid}/live-run", json={"op_id": op})
    assert r.status_code == 200, r.text
    _wait(app, c, r.json()["job"]["id"])
    st = c.get(f"/api/farms/{fid}/live-run").json()
    assert st["source"] == "simulated camera" and st["status"] == "done"
    assert st["sorties"] and st["planned"]["litres"] > 0 and st["flown"]["litres"] > 0
    assert "remaining" in st and st["drone"] is not None and st["detections"]
    ops = {o["id"]: o for o in c.get(f"/api/farms/{fid}/session").json()["ops"]}
    assert ops[op]["status"] == "flown"


@pytest.fixture
def live_run(app, herb):
    c, fid, op = herb
    tok = c.post(f"/api/farms/{fid}/devices", json={"kind": "camera", "name": "sprayer camera"}).json()["token"]
    r = c.post(f"/api/farms/{fid}/live-run", json={"op_id": op, "source": "aircraft"})
    assert r.status_code == 200, r.text
    return c, fid, tok, r.json()["run"]["run_id"]


def _batch(run, seq, cells=(), telemetry=()):
    return {"schema": SCHEMA, "run_id": run, "seq": seq, "sent_at": "2025-06-05T07:00:00Z",
            "telemetry": list(telemetry), "cells": list(cells)}


def _cell(i, weeds=3):
    return {"t": "2025-06-05T07:00:00Z", "cell": i, "weeds": weeds, "area_m2": 100.0, "gsd_m": 0.002, "blight": False}


def test_replayed_and_out_of_order_batches_count_once(live_run):
    c, fid, tok, run = live_run
    b1, b2 = _batch(run, 1, [_cell(1)]), _batch(run, 2, [_cell(2)])
    outs = [c.post("/api/live-run/ingest", json=b, headers={"X-Device-Token": tok}).json() for b in (b2, b1, b2, b1)]
    assert [o["duplicate"] for o in outs] == [False, False, True, True]
    st = c.get(f"/api/farms/{fid}/live-run").json()
    assert st["cells_seen"] == 2 and st["batches"] == 2


def test_ingest_needs_a_device_token(live_run):
    c, fid, tok, run = live_run
    assert c.post("/api/live-run/ingest", json=_batch(run, 1)).status_code == 401
    assert c.post("/api/live-run/ingest", json=_batch(run, 1), headers={"X-Device-Token": "nope"}).status_code == 401


def test_bad_items_are_reported_and_the_rest_kept(live_run):
    c, fid, tok, run = live_run
    r = c.post("/api/live-run/ingest", json=_batch(run, 1, [_cell(1), _cell(99_999), _cell(2, weeds=-4)]),
               headers={"X-Device-Token": tok}).json()
    assert r["accepted"] == 1 and len(r["errors"]) == 2


def test_weedy_cells_seen_by_the_aircraft_join_the_plan(live_run):
    c, fid, tok, run = live_run
    st0 = c.get(f"/api/farms/{fid}/live-run").json()
    clean = [i for i in range(100) if i not in st0["remaining"]][:10]
    c.post("/api/live-run/ingest", json=_batch(run, 1, [_cell(i, weeds=4000) for i in clean]),
           headers={"X-Device-Token": tok})
    st = c.get(f"/api/farms/{fid}/live-run").json()
    assert set(clean) <= set(st["remaining"]) and st["audit"]
    assert any(a["channel"] == "weeds" for a in st["audit"])


def test_freeze_stops_corrections_and_is_audited(live_run):
    c, fid, tok, run = live_run
    st0 = c.get(f"/api/farms/{fid}/live-run").json()
    assert c.post(f"/api/farms/{fid}/live-run/freeze").status_code == 200
    clean = [i for i in range(100) if i not in st0["remaining"]][:10]
    c.post("/api/live-run/ingest", json=_batch(run, 1, [_cell(i, weeds=4000) for i in clean]),
           headers={"X-Device-Token": tok})
    st = c.get(f"/api/farms/{fid}/live-run").json()
    assert st["mode"] == "open loop" and not set(clean) & set(st["remaining"])
    assert any(a["correction"] == "plan frozen by the operator" for a in st["audit"])


def test_next_sortie_kml_covers_remaining_targets(live_run):
    c, fid, _, _ = live_run
    r = c.get(f"/api/farms/{fid}/live-run/next-sortie.kml")
    assert r.status_code == 200 and b"<Polygon>" in r.content


def test_finish_assimilates_what_was_seen_and_survives_restore(app, live_run):
    c, fid, tok, run = live_run
    c.post("/api/live-run/ingest", json=_batch(run, 1, [_cell(i, weeds=0) for i in range(20)]),
           headers={"X-Device-Token": tok})
    r = c.post(f"/api/farms/{fid}/live-run/finish")
    assert r.status_code == 200, r.text
    wm = app.state.sessions.get(fid).twin.digital.weed_map.copy()
    app.state.sessions._sessions.pop(fid)                    # forces a restore from the action log
    assert (app.state.sessions.get(fid).twin.digital.weed_map == wm).all()
    assert c.get(f"/api/farms/{fid}/live-run").json()["status"] == "finished"
