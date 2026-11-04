"""The console flying the real drone: the K3-A Pro agent's command channel (with the actual
fa-agent flying the console's job on the simulated K3-A Pro) and ArduPilot over MAVLink
(against ArduCopter SITL when available)."""
import time

import pytest

from fa_agent.route import Job
from fa_agent.simrun import run
from fa_agent.uplink import ConsoleLink
from test_console_farms import _create, app, fake_builder  # noqa: F401  (fixture)
from test_console_live_run import _weedy_builder, herb  # noqa: F401  (fixture)


class _Http:
    """requests-like adapter over the TestClient (what ConsoleLink uses on the drone)."""
    def __init__(self, c):
        self.c = c

    def get(self, url, params=None, headers=None, timeout=None):
        return self.c.get(url.replace("http://console", ""), params=params, headers=headers)

    def post(self, url, json=None, headers=None, timeout=None):
        return self.c.post(url.replace("http://console", ""), json=json, headers=headers)


@pytest.fixture
def agent(app, herb):
    c, fid, op = herb
    tok = c.post(f"/api/farms/{fid}/devices", json={"kind": "drone_agent", "name": "K3 agent"}).json()["token"]
    return c, fid, op, tok


def test_the_job_the_console_builds_is_valid_for_the_aircraft(app, herb):
    c, fid, op = herb
    jd = c.get(f"/api/farms/{fid}/aircraft/job-preview", params={"op_id": op}).json()
    job = Job.from_json(jd)
    s = app.state.sessions.get(fid)
    per_sortie = [sum(p.length_m for p in so) * s.flight.swath_m * s.flight.rate_l_ha / 1e4 for so in job.sorties]
    assert jd["summary"]["passes"] >= 1 and all(lit <= s.spec.tank_l for lit in per_sortie)


def test_agent_needs_its_own_token(agent):
    c, fid, op, tok = agent
    assert c.get("/api/agent/poll", params={"wait": 0}).status_code == 401
    cam = c.post(f"/api/farms/{fid}/devices", json={"kind": "camera", "name": "cam"}).json()["token"]
    assert c.get("/api/agent/poll", params={"wait": 0}, headers={"X-Device-Token": cam}).status_code == 401


def test_console_to_agent_end_to_end_on_the_simulated_k3(agent):
    """Operator: Start job -> the agent's link picks it up -> the real virtual pilot flies the
    console's job on the simulated K3-A Pro -> its state comes back to the console."""
    c, fid, op, tok = agent
    r = c.post(f"/api/farms/{fid}/aircraft/agent/command", json={"kind": "start_job", "op_id": op})
    assert r.status_code == 200, r.text
    link = ConsoleLink("http://console", tok, session=_Http(c))
    cmds = link.poll(1)
    assert [x["kind"] for x in cmds] == ["start_job"] and cmds[0]["run_id"].startswith("run_")
    job = Job.from_json(cmds[0]["job"])
    link.ack(cmds[0]["id"], True)
    res = run(job, max_s=1500)
    assert res.state == "DONE", res.summary()
    assert res.xtrack_p95 < 0.6 and res.spray_s > 0
    assert link.state({"state": res.state, "events": [[1.0, "DONE", "job complete"]]})
    v = c.get(f"/api/farms/{fid}/aircraft/agent").json()
    assert v["online"] and v["agent"]["state"] == "DONE" and v["acks"][0]["ok"]
    assert c.get(f"/api/farms/{fid}/live-run").json()["source"] == "aircraft"


def test_abort_is_queued_for_the_agent(agent):
    c, fid, op, tok = agent
    c.post(f"/api/farms/{fid}/aircraft/agent/command", json={"kind": "abort"})
    got = c.get("/api/agent/poll", params={"wait": 0}, headers={"X-Device-Token": tok}).json()["commands"]
    assert [x["kind"] for x in got] == ["abort"]
    assert c.post(f"/api/farms/{fid}/aircraft/agent/command", json={"kind": "selfdestruct"}).status_code == 422


def test_no_flight_controller_is_a_clear_404(agent):
    c, fid, op, tok = agent
    assert c.get(f"/api/farms/{fid}/aircraft/ardupilot/preflight").status_code == 404
    r = c.post(f"/api/farms/{fid}/aircraft/ardupilot/connect", json={"link": "bluetooth:x"})
    assert r.status_code == 422


@pytest.mark.skipif(__import__("fa_aircraft.sitl", fromlist=["find_sitl"]).find_sitl() is None,
                    reason="no ArduCopter SITL binary (set FA_SITL)")
def test_console_flies_the_job_on_ardupilot_sitl(app, herb):
    from fa_aircraft.sitl import Sitl, find_sitl
    c, fid, op = herb
    jd = c.get(f"/api/farms/{fid}/aircraft/job-preview", params={"op_id": op}).json()
    h = jd["home"]
    sitl = Sitl(find_sitl(), home=(h["lat"], h["lon"], 200.0, 0.0), speedup=8, instance=3).start()
    try:
        r = c.post(f"/api/farms/{fid}/aircraft/ardupilot/connect", json={"link": f"tcp:127.0.0.1:{sitl.port}"})
        assert r.status_code == 200, r.text
        svc = app.state.ardupilot
        v = svc._v[fid]
        v.heartbeat_hz = 10                                     # sim time runs 8x
        from fa_aircraft.runner import SAFETY_PARAMS, load_param_file
        for k, val in load_param_file(keys=SAFETY_PARAMS).items():
            v.set_param(k, val)
        end = time.monotonic() + 120
        while (v.state.ekf_flags & 0x1F) != 0x1F or v.state.gps_fix < 3:
            assert time.monotonic() < end
            time.sleep(0.5)
        import fa_aircraft.runner as R
        real = R.preflight
        R.preflight = lambda v, *a, **k: real(v, 3.5, 3, *a[2:], **k)   # SITL flies a 3S battery
        try:
            assert c.post(f"/api/farms/{fid}/aircraft/ardupilot/start", json={"op_id": op}).status_code == 200
            end = time.monotonic() + 600
            while svc.status(fid)["job"]["flying"]:
                assert time.monotonic() < end
                if svc.status(fid)["job"]["state"] == "WAIT_SWAP":
                    c.post(f"/api/farms/{fid}/aircraft/ardupilot/battery-swapped")
                time.sleep(1.0)
        finally:
            R.preflight = real
        st = c.get(f"/api/farms/{fid}/aircraft/ardupilot").json()
        assert st["job"]["state"] == "DONE", st["job"]["events"]
        assert c.get(f"/api/farms/{fid}/live-run").json()["batches"] > 5
    finally:
        app.state.ardupilot.disconnect(fid)
        sitl.stop()
