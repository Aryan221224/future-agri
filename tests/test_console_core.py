"""Console core: SQLite store, persistent job queue, remote-worker protocol, app skeleton."""
import json
import time

from fastapi.testclient import TestClient

from fa_console.app import create_app
from fa_console.config import Settings
from fa_console.jobs import JobManager
from fa_console.store import Store


def _settings(tmp_path, **kw):
    return Settings(home=tmp_path / "home", cache=tmp_path / "cache", **kw)


def _mgr(tmp_path):
    s = _settings(tmp_path)
    s.home.mkdir(parents=True, exist_ok=True)
    st = Store(s.db_path)
    return st, JobManager(st, s)


def test_store_farm_crud_and_events(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    f = st.create_farm({"name": "North field", "mode": "sim", "template": "A5", "lat": 52.5,
                        "lon": 5.6, "meta": {"x": 1}})
    assert f["id"] and f["status"] == "new" and f["meta"] == {"x": 1}
    st.update_farm(f["id"], status="ready", area_ha=15.1)
    g = st.get_farm(f["id"])
    assert g["status"] == "ready" and g["area_ha"] == 15.1
    st.add_event(f["id"], "built", {"cells": 10})
    ev = st.events(f["id"])
    assert ev[-1]["kind"] == "built" and ev[-1]["payload"]["cells"] == 10 and ev[-1]["actor"] == "operator"
    assert [x["id"] for x in st.list_farms()] == [f["id"]]
    st.delete_farm(f["id"])
    assert st.get_farm(f["id"]) is None and st.events(f["id"]) == []


def test_job_runs_and_reports_progress(tmp_path):
    st, jm = _mgr(tmp_path)
    seen = []

    def fn(job, ctx):
        ctx.progress(0.5, "half")
        seen.append(st.get_job(job["id"])["progress"])
        return {"summary": {"answer": job["params"]["x"] * 2}, "big": list(range(5))}

    jm.register("double", fn)
    j = jm.submit("double", None, {"x": 21})
    assert j["status"] == "queued" and j["target"] == "local"
    jm.run_pending()
    d = st.get_job(j["id"])
    assert d["status"] == "done" and d["progress"] == 1.0 and seen == [0.5]
    assert d["result"] == {"answer": 42}
    assert jm.result(j["id"])["big"] == [0, 1, 2, 3, 4]


def test_job_failure_and_cancel(tmp_path):
    st, jm = _mgr(tmp_path)
    jm.register("boom", lambda job, ctx: 1 / 0)
    j = jm.submit("boom", None, {})
    jm.run_pending()
    d = st.get_job(j["id"])
    assert d["status"] == "failed" and "ZeroDivisionError" in d["error"]

    def slow(job, ctx):
        jm.cancel(job["id"])                 # the operator cancels while it runs
        ctx.progress(0.1, "still going")     # the next progress call stops the job
        return {}

    jm.register("slow", slow)
    k = jm.submit("slow", None, {})
    jm.run_pending()
    assert st.get_job(k["id"])["status"] == "cancelled"
    q = jm.submit("slow", None, {})
    jm.cancel(q["id"])                       # still queued: cancelled at once
    assert st.get_job(q["id"])["status"] == "cancelled"


def test_unknown_job_kind_rejected(tmp_path):
    _, jm = _mgr(tmp_path)
    try:
        jm.submit("nope", None, {})
    except ValueError as e:
        assert "nope" in str(e)
    else:
        raise AssertionError("unknown kind accepted")


def test_restart_marks_running_jobs_failed(tmp_path):
    st, jm = _mgr(tmp_path)
    jm.register("x", lambda job, ctx: {})
    j = jm.submit("x", None, {})
    st.update_job(j["id"], status="running")
    JobManager(st, jm.settings)              # a new process starts
    d = st.get_job(j["id"])
    assert d["status"] == "failed" and "server restarted" in d["error"]


def test_remote_worker_claim_requires_token(tmp_path):
    app = create_app(_settings(tmp_path), start_workers=False)
    with TestClient(app) as c:
        jm = app.state.jobs
        jm.register("remote_echo", lambda job, ctx: {"local": True}, remote=True)
        tok = c.get("/api/workers/token").json()["token"]
        bad = c.post("/api/workers/claim", json={"token": "nope", "worker": "h200", "kinds": ["remote_echo"]})
        assert bad.status_code == 401
        # once a remote worker has been seen, 'auto' jobs of its kinds go to it
        r = c.post("/api/workers/claim", json={"token": tok, "worker": "h200", "kinds": ["remote_echo"]})
        assert r.status_code == 204
        j = jm.submit("remote_echo", None, {"v": 1})
        assert j["target"] == "remote"
        r = c.post("/api/workers/claim", json={"token": tok, "worker": "h200", "kinds": ["remote_echo"]})
        assert r.status_code == 200 and r.json()["id"] == j["id"]
        assert c.post(f"/api/workers/{j['id']}/progress",
                      json={"token": tok, "progress": 0.5, "message": "half"}).status_code == 200
        assert c.post(f"/api/workers/{j['id']}/complete",
                      json={"token": tok, "result": {"summary": {"ok": 1}}}).status_code == 200
        d = c.get(f"/api/jobs/{j['id']}").json()
        assert d["status"] == "done" and d["worker"] == "h200" and d["result"] == {"ok": 1}
        assert [w["name"] for w in c.get("/api/workers").json()["remote"]] == ["h200"]


def test_health_meta_and_spa_fallback(tmp_path):
    app = create_app(_settings(tmp_path), start_workers=False)
    with TestClient(app) as c:
        h = c.get("/api/health").json()
        assert h["ok"] is True and h["lan_exposed"] is False
        m = c.get("/api/meta").json()
        assert {"A1", "A2", "A3", "A4", "A5", "A6"} <= {t["id"] for t in m["templates"]}
        assert any(r["id"] == "EU" for r in m["regions"])
        r = c.get("/farms/whatever")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert c.get("/api/nope").status_code == 404
        assert c.get("/api/sources").json()["sources"]
    lan = create_app(_settings(tmp_path / "lan", bind="0.0.0.0"), start_workers=False)
    with TestClient(lan) as c:
        assert c.get("/api/health").json()["lan_exposed"] is True


def test_sse_streams_job_events(tmp_path):
    app = create_app(_settings(tmp_path), start_workers=True)
    with TestClient(app) as c:
        jm = app.state.jobs

        def fn(job, ctx):
            for i in range(3):
                ctx.progress((i + 1) / 4, f"step {i}")
                time.sleep(0.15)
            return {"summary": {"n": 3}}

        jm.register("steps", fn)
        j = jm.submit("steps", None, {})
        with c.stream("GET", f"/api/jobs/{j['id']}/events") as r:
            lines = [ln for ln in r.iter_lines() if ln.startswith("data:")]
        evs = [json.loads(ln[5:]) for ln in lines]
        assert evs[-1]["status"] == "done"
        assert any(e["message"].startswith("step") for e in evs)


def test_lost_remote_worker_fails_its_job(tmp_path):
    st, jm = _mgr(tmp_path)
    jm.register("far", lambda job, ctx: {}, remote=True)
    tok = jm.worker_token()
    jm.claim(tok, "h200", ["far"])
    j = jm.submit("far", None, {})
    assert j["target"] == "remote"
    assert jm.claim(tok, "h200", ["far"])["id"] == j["id"]
    st.update_job(j["id"], heartbeat_at="2000-01-01T00:00:00+00:00")
    jm._watchdog()
    d = st.get_job(j["id"])
    assert d["status"] == "failed" and "worker" in d["error"]
