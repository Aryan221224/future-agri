"""Iterations (the perfect-run job) and plans with timings; approving configures the mission."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fa_console.plans import logistics_pick, make_plan
from test_console_farms import _create, app, fake_builder  # noqa: F401  (fixture)

FIX = Path(__file__).parent / "fixtures" / "perfect_small.json"


def _result():
    return json.loads(FIX.read_text())


def _farm_meta(result):
    return {"id": "f_test", "sowing": result["rehearsal"]["sowing"], "harvest": result["rehearsal"]["harvest"],
            "mode": "sim", "region": "EU", "area_ha": result["area_ha"]}


def test_make_plan_operations_and_totals():
    res = _result()
    plan = make_plan(_farm_meta(res), res, None)
    assert plan["plan"] == res["best"]["plan"] and plan["policy"] == res["best"]["policy"]
    assert plan["currency"] == res["currency"] and plan["net_mean"] == res["best"]["net_mean"]
    assert {o["product"] for o in plan["operations"]} <= {"herbicide", "fungicide", "scan"}
    assert plan["scan"]["das"] == res["best"]["scan_das"]
    sprayed = 0
    for row in res["ranked"]:                       # every candidate plan, not only the best
        pl = make_plan(_farm_meta(res), res, row["plan"])
        drone_h = 0.0
        for o in pl["operations"]:
            assert o["count_mean"] >= 0
            if o["product"] != "scan" and o["count_mean"] > 0:
                sprayed += 1
                assert o["das_first"] <= o["das_median"] <= o["das_last"]
                assert o["date_first"] <= o["date_last"]
                assert o["crew_min"] == pytest.approx(o["time_min"] + 20, abs=0.05)
                drone_h += o["count_mean"] * o["time_min"] / 60
        assert pl["totals"]["drone_h"] == pytest.approx(drone_h, abs=0.02)
        if row["policy"].startswith("twin"):
            assert pl["operations"][0]["product"] == "scan"
    assert sprayed >= 3                             # calendar and twin plans fly jobs
    other = next(r["plan"] for r in res["ranked"] if r["plan"] != res["best"]["plan"])
    assert make_plan(_farm_meta(res), res, other)["plan"] == other
    with pytest.raises(ValueError):
        make_plan(_farm_meta(res), res, "no-such-plan")


def test_logistics_pick_rule():
    rows = [{"battery_sets": 4, "chargers": 1, "ha_per_hour": 2.9},
            {"battery_sets": 6, "chargers": 1, "ha_per_hour": 3.0},
            {"battery_sets": 4, "chargers": 2, "ha_per_hour": 4.1},
            {"battery_sets": 6, "chargers": 2, "ha_per_hour": 4.4}]
    pick = logistics_pick(rows)
    assert (pick["battery_sets"], pick["chargers"]) == (4, 2)       # within 90 % of 4.4, cheaper than 6+2
    assert logistics_pick([]) is None


def _inject_result(app, fid, res):
    st = app.state.store
    job = st.create_job("perfect", fid, {"seeds": 1}, target="local")
    (app.state.settings.jobs_dir / f"{job['id']}.json").write_text(json.dumps(res))
    st.update_job(job["id"], status="done", progress=1.0, result=res["summary"])
    return job["id"]


def test_approve_supersedes_and_configures_session(app):
    res = _result()
    app.state.sessions.defaults["plan_members"] = 4
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        jid = _inject_result(app, fid, res)
        r = c.post(f"/api/farms/{fid}/plans", json={"job_id": jid})
        assert r.status_code == 200, r.text
        p1 = r.json()
        assert p1["status"] == "draft" and p1["payload"]["plan"] == res["best"]["plan"]
        assert c.post(f"/api/plans/{p1['id']}/approve", json={}).status_code == 200
        s = c.get(f"/api/farms/{fid}/session").json()
        assert s["plan"]["id"] == p1["id"] and s["policy"] == res["best"]["policy"]
        assert s["scan_das"] == res["best"]["scan_das"]
        # progress the mission, then a second plan needs confirmation because it resets it
        assert c.post(f"/api/farms/{fid}/session/step").status_code == 200
        other = next(x["plan"] for x in res["ranked"] if x["plan"] != res["best"]["plan"])
        p2 = c.post(f"/api/farms/{fid}/plans", json={"job_id": jid, "plan": other}).json()
        r = c.post(f"/api/plans/{p2['id']}/approve", json={})
        assert r.status_code == 409 and "reset" in r.text
        r = c.post(f"/api/plans/{p2['id']}/approve", json={"confirm_reset": True})
        assert r.status_code == 200
        plans = {p["id"]: p["status"] for p in c.get(f"/api/farms/{fid}/plans").json()["plans"]}
        assert plans == {p1["id"]: "superseded", p2["id"]: "approved"}
        s = c.get(f"/api/farms/{fid}/session").json()
        assert s["plan"]["id"] == p2["id"] and s["n_events"] == 0


def test_plan_from_unfinished_job_rejected(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        j = app.state.store.create_job("perfect", f["id"], {}, target="local")
        r = c.post(f"/api/farms/{f['id']}/plans", json={"job_id": j["id"]})
        assert r.status_code == 422 and "not finished" in r.text


@pytest.mark.slow
def test_perfect_job_quick_runs(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        r = c.post(f"/api/farms/{f['id']}/perfect", json={"seeds": 1, "members": 4, "quick": True,
                                                          "workers": 1})
        assert r.status_code == 200, r.text
        app.state.jobs.run_pending()
        job = c.get(f"/api/jobs/{r.json()['id']}").json()
        assert job["status"] == "done", job
        res = c.get(f"/api/jobs/{job['id']}/result").json()
        assert res["ranked"] and res["best"]["plan"] and res["seasons_summary"]
        assert res["rehearsal"]["sowing"] and res["flight_ranking"]
        if not FIX.exists():                         # record the fixture the fast tests use
            FIX.write_text(json.dumps(res))
