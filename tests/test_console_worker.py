"""Remote GPU worker: claims a job over HTTP, runs it with the console's own job
function on a downloaded copy of the farm, reports progress and completes it."""
import json
import socket
import threading
import time
import urllib.request

import pytest

import fa_console.worker as worker
from fa_console.app import create_app
from fa_console.config import Settings
from fa_console.routes.drone import MissionIn
from test_console_farms import _square, fake_builder


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def served(tmp_path):
    import uvicorn
    app = create_app(Settings(home=tmp_path / "home", cache=tmp_path / "cache", offline=True), start_workers=False)
    app.state.farms.builder = fake_builder
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    yield app, base
    server.should_exit = True
    t.join(timeout=5)


def _farm(app, base) -> str:
    body = json.dumps({"name": "GPU field", "mode": "sim", "template": "A5", "polygon": _square(100),
                       "year": 2025}).encode()
    req = urllib.request.Request(base + "/api/farms", data=body, headers={"Content-Type": "application/json"})
    fid = json.loads(urllib.request.urlopen(req).read())["farm"]["id"]
    app.state.jobs.run_pending()
    assert app.state.store.get_farm(fid)["status"] == "ready"
    return fid


def test_worker_claims_and_completes_quick_job(served, tmp_path):
    app, base = served
    fid = _farm(app, base)
    job = app.state.jobs.submit("mission", fid, MissionIn().model_dump(), target="remote")
    tok = app.state.jobs.worker_token()
    rc = worker.main(["--server", base, "--token", tok, "--kinds", "mission", "--once",
                      "--home", str(tmp_path / "w"), "--name", "h200-test"])
    assert rc == 0
    d = app.state.store.get_job(job["id"])
    assert d["status"] == "done", d
    assert d["worker"] == "h200-test" and d["result"]["sorties"] >= 1
    res = app.state.jobs.result(job["id"])
    assert res["feasible"] in (True, False) and res["kpis"]["time_min"] > 0
    w = app.state.jobs.workers()["remote"][0]
    assert w["name"] == "h200-test" and w["kinds"] == ["mission"] and w["info"]["python"]
    # the farm was fetched once and cached for the next job
    assert (tmp_path / "w" / "farms" / fid / "farm.pkl").exists()


def test_worker_with_no_job_exits_cleanly_and_bad_token_fails(served, tmp_path):
    app, base = served
    tok = app.state.jobs.worker_token()
    assert worker.main(["--server", base, "--token", tok, "--kinds", "perfect,mission", "--once",
                        "--home", str(tmp_path / "w")]) == 0
    assert {w["name"] for w in app.state.jobs.workers()["remote"]}           # it was seen
    assert worker.main(["--server", base, "--token", "wrong", "--once", "--home", str(tmp_path / "w")]) == 2


def test_worker_token_file_is_read_and_removed(served, tmp_path):
    app, base = served
    tf = tmp_path / "worker.token"
    tf.write_text(app.state.jobs.worker_token() + "\n")
    assert worker.main(["--server", base, "--token-file", str(tf), "--once", "--home", str(tmp_path / "w")]) == 0
    assert not tf.exists()


def test_detect_not_advertised_without_weights(monkeypatch, tmp_path):
    monkeypatch.delenv("FA_DETECTOR_WEIGHTS", raising=False)
    assert worker.available_kinds(["perfect", "mission", "detect"]) == ["perfect", "mission"]
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("FA_DETECTOR_WEIGHTS", str(run))
    assert "detect" not in worker.available_kinds(["detect"])                 # no run.json
    (run / "run.json").write_text(json.dumps({"classes": ["maize", "weed"], "resolution": 576}))
    monkeypatch.setattr(worker, "_backend_available", lambda: False)
    assert "detect" not in worker.available_kinds(["detect"])                 # no inference backend
    monkeypatch.setattr(worker, "_backend_available", lambda: True)
    assert worker.available_kinds(["detect", "perfect"]) == ["detect", "perfect"]
    assert worker.available_kinds(["nope", "mission"]) == ["mission"]          # unknown kinds dropped


def test_worker_stops_when_the_tunnel_is_gone(tmp_path):
    t0 = time.monotonic()
    rc = worker.main(["--server", f"http://127.0.0.1:{_free_port()}", "--token", "x", "--poll", "0.05",
                      "--give-up-after", "0.4", "--home", str(tmp_path / "w")])
    assert rc == 4 and time.monotonic() - t0 < 10
