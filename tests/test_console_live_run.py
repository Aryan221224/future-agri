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


# ---------------------------------------------------------------- review findings
def _tel(lon, lat, sortie=1, spraying=True, t="2025-06-05T07:00:00Z"):
    return {"t": t, "lat": lat, "lon": lon, "alt_agl_m": 2.5, "heading_deg": 90.0, "speed_ms": 5.0,
            "spraying": spraying, "sortie": sortie}


def _pass_points(app, fid, n=12):
    """A straight spraying pass along a middle row of cell centres of the farm's grid."""
    import numpy as np
    from pyproj import Transformer

    from fa_console.scene import farm_grid
    grid = farm_grid(app.state.farms.load(fid))
    xy = grid.cells_xy
    y = float(np.median(np.unique(xy[:, 1])))
    x0, x1 = xy[:, 0].min(), xy[:, 0].max()
    tr = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    out = []
    for i in range(n):
        lon, lat = tr.transform(x0 + (x1 - x0) * i / (n - 1), y)
        out.append(_tel(lon, lat, t=f"2025-06-05T07:00:{i:02d}Z"))
    return out


def test_i3_a_pass_split_over_batches_doses_the_whole_strip(app, live_run):
    import numpy as np
    from pyproj import Transformer

    from fa_console.scene import farm_grid
    from fa_scan.sprayer_camera import cells_along
    c, fid, tok, run = live_run
    pts = _pass_points(app, fid)
    for k in range(6):                                       # one segment per batch: every gap is a segment
        c.post("/api/live-run/ingest", json=_batch(run, k, telemetry=pts[2 * k:2 * k + 2]),
               headers={"X-Device-Token": tok})
    grid = farm_grid(app.state.farms.load(fid))
    tr = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    local = [{"x": tr.transform(p["lon"], p["lat"])[0], "y": tr.transform(p["lon"], p["lat"])[1],
              "spraying": True, "sortie": 1} for p in pts]
    swath = app.state.live_run._get(fid)["swath"]
    expected = cells_along(local, grid, swath, sortie=1)
    dosed = np.asarray(app.state.live_run._get(fid)["dose"]) > 0
    assert expected.any() and (dosed == expected).all()


def test_i4_telemetry_far_outside_the_field_is_rejected(live_run):
    c, fid, tok, run = live_run
    r = c.post("/api/live-run/ingest", json=_batch(run, 1, telemetry=[_tel(0.0, 0.0)]),
               headers={"X-Device-Token": tok}).json()
    assert r["accepted"] == 0 and any("outside the field" in e for e in r["errors"])
    assert c.get(f"/api/farms/{fid}/live-run").json()["drone"] is None


def test_i5_a_second_run_cannot_replace_a_running_one(live_run):
    c, fid, tok, run = live_run
    c.post("/api/live-run/ingest", json=_batch(run, 1, [_cell(1)]), headers={"X-Device-Token": tok})
    op = c.get(f"/api/farms/{fid}/live-run").json()["op_id"]
    r = c.post(f"/api/farms/{fid}/live-run", json={"op_id": op, "source": "aircraft"})
    assert r.status_code == 409
    st = c.get(f"/api/farms/{fid}/live-run").json()
    assert st["run_id"] == run and st["cells_seen"] == 1


def test_i6_simulated_run_on_a_busy_session_fails_visibly(app, herb):
    c, fid, op = herb
    lock = app.state.sessions._lock(fid)
    lock.acquire()
    try:
        c.post(f"/api/farms/{fid}/live-run", json={"op_id": op})
        app.state.jobs.run_pending()
    finally:
        lock.release()
    st = c.get(f"/api/farms/{fid}/live-run").json()
    assert st["status"] == "failed" and "busy" in (st["reason"] or "")
    assert c.post(f"/api/farms/{fid}/live-run", json={"op_id": op}).status_code == 200   # can start again


def test_i6_finish_on_a_busy_session_can_be_retried(app, live_run):
    c, fid, tok, run = live_run
    c.post("/api/live-run/ingest", json=_batch(run, 1, [_cell(i, weeds=0) for i in range(10)]),
           headers={"X-Device-Token": tok})
    lock = app.state.sessions._lock(fid)
    lock.acquire()
    try:
        assert c.post(f"/api/farms/{fid}/live-run/finish").status_code == 409
    finally:
        lock.release()
    assert c.get(f"/api/farms/{fid}/live-run").json()["status"] == "running"
    assert c.post(f"/api/farms/{fid}/live-run/finish").status_code == 200


def test_i7_finish_books_the_job_without_a_survey_scan(app, live_run):
    c, fid, tok, run = live_run
    s = app.state.sessions.get(fid)
    scans0, scan_cost0 = s.st["scans"], s.costs["scans"]
    op = c.get(f"/api/farms/{fid}/live-run").json()["op_id"]
    c.post("/api/live-run/ingest", json=_batch(run, 1, [_cell(i, weeds=2) for i in range(10)], _pass_points(app, fid)),
           headers={"X-Device-Token": tok})
    assert c.post(f"/api/farms/{fid}/live-run/finish").status_code == 200
    s = app.state.sessions.get(fid)
    assert s.ops[op].status == "flown" and s.dose_cum["herbicide"].sum() > 0
    assert s.st["scans"] == scans0 and s.costs["scans"] == scan_cost0
    app.state.sessions._sessions.pop(fid)                    # replays identically
    r = app.state.sessions.get(fid)
    assert r.ops[op].status == "flown" and (r.dose_cum["herbicide"] == s.dose_cum["herbicide"]).all()


def test_i9_a_batch_that_failed_to_apply_is_not_lost(app, live_run, monkeypatch):
    c, fid, tok, run = live_run
    svc = app.state.live_run
    real = svc._apply
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full")
        return real(*a, **k)
    monkeypatch.setattr(svc, "_apply", flaky)
    with pytest.raises(RuntimeError):
        c.post("/api/live-run/ingest", json=_batch(run, 1, [_cell(3)]), headers={"X-Device-Token": tok})
    r = c.post("/api/live-run/ingest", json=_batch(run, 1, [_cell(3)]), headers={"X-Device-Token": tok}).json()
    assert r["duplicate"] is False and c.get(f"/api/farms/{fid}/live-run").json()["cells_seen"] == 1
