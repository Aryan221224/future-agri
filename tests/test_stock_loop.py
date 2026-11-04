"""Closed loop with the stock K3-A Pro (no added hardware): blocks, flight records, re-plan,
booking, and the Agri Assistant remote over ADB."""
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient  # noqa: F401

from fa_console.rc_link import RcError, RcLink
from test_console_farms import _create, app, fake_builder  # noqa: F401  (fixture)
from test_console_live_run import _weedy_builder, herb  # noqa: F401  (fixture)


@pytest.fixture
def stock(app, herb):
    c, fid, op = herb
    r = c.post(f"/api/farms/{fid}/stock-loop", json={"op_id": op, "tank_l": 1.0})
    assert r.status_code == 200, r.text
    return c, fid, op, r.json()


def _rec(st, n, share=1.0, dose=None):
    b = st["blocks"][n - 1]
    area = b["area_ha"] * share
    return {"litres": round(area * (dose or st["rate_l_ha"]), 4), "area_ha": round(area, 5), "flight_min": 4.0,
            "block": n}


def test_the_job_is_split_into_tank_sized_blocks_with_a_kml_each(stock):
    c, fid, op, st = stock
    assert st["status"] == "running" and st["current"] == 1 and len(st["blocks"]) >= 2
    assert all(b["litres_planned"] <= 1.0 for b in st["blocks"])
    r = c.get(f"/api/farms/{fid}/stock-loop/block.kml?n=1")
    assert r.status_code == 200 and b"<Polygon>" in r.content


def test_a_full_record_closes_the_block_and_moves_on(stock):
    c, fid, op, st = stock
    st = c.post(f"/api/farms/{fid}/stock-loop/record", json=_rec(st, 1)).json()
    assert st["blocks"][0]["status"] == "done" and st["current"] == 2


def test_a_short_record_keeps_the_block_for_a_resume(stock):
    c, fid, op, st = stock
    st = c.post(f"/api/farms/{fid}/stock-loop/record", json=_rec(st, 1, share=0.5)).json()
    assert st["blocks"][0]["status"] == "partial" and st["current"] == 1
    assert any("breakpoint" in a["reason"] for a in st["audit"])
    st = c.post(f"/api/farms/{fid}/stock-loop/record", json=_rec(st, 1, share=0.5)).json()
    assert st["blocks"][0]["status"] == "done" and st["current"] == 2


def test_an_overdose_is_flagged(stock):
    c, fid, op, st = stock
    st = c.post(f"/api/farms/{fid}/stock-loop/record", json=_rec(st, 1, dose=3 * st["rate_l_ha"])).json()
    assert any("150 %" in a["reason"] for a in st["audit"])


def test_the_twin_drops_blocks_it_no_longer_wants(app, stock, monkeypatch):
    c, fid, op, st = stock
    s = app.state.sessions.get(fid)
    keep = np.zeros(s.grid.n_cells, bool)
    keep[app.state.stock_loop._get(fid)["blocks"][0]["cells"]] = True     # only block 1 still wanted
    monkeypatch.setattr(s.twin, "herbicide_targets", lambda w: ("spot", keep))
    st = c.post(f"/api/farms/{fid}/stock-loop/record", json=_rec(st, 1)).json()
    assert all(b["status"] == "dropped" for b in st["blocks"][1:]) and st["current"] is None


def test_finish_books_the_job_and_replays(app, stock):
    c, fid, op, st = stock
    s = app.state.sessions.get(fid)
    scans0 = s.st["scans"]
    c.post(f"/api/farms/{fid}/stock-loop/record", json=_rec(st, 1))
    assert c.post(f"/api/farms/{fid}/stock-loop/finish").status_code == 200
    s = app.state.sessions.get(fid)
    assert s.ops[op].status == "flown" and s.dose_cum["herbicide"].sum() > 0 and s.st["scans"] == scans0
    app.state.sessions._sessions.pop(fid)
    r = app.state.sessions.get(fid)
    assert (r.dose_cum["herbicide"] == s.dose_cum["herbicide"]).all()
    assert c.get(f"/api/farms/{fid}/stock-loop").json()["status"] == "finished"


def test_a_running_stock_job_is_not_replaced(stock):
    c, fid, op, st = stock
    assert c.post(f"/api/farms/{fid}/stock-loop", json={"op_id": op}).status_code == 409


# ---------------------------------------------------------------- the remote over ADB
class FakeAdb:
    def __init__(self, devices="R5CT1 device\n", files=None):
        self.devs, self.files, self.pushed, self.calls = devices, dict(files or {}), {}, []

    def __call__(self, cmd, capture_output=True, timeout=None, text=True):
        self.calls.append(cmd)
        a = cmd[1:]
        if a[0] == "-s":
            a = a[2:]
        ok = lambda out: subprocess.CompletedProcess(cmd, 0, out, "" if text else b"")  # noqa: E731
        if a[0] == "devices":
            return ok("List of devices attached\n" + self.devs)
        if "unauthorized" in self.devs:
            return subprocess.CompletedProcess(cmd, 1, "", "error: device unauthorized.")
        if a[:2] == ["shell", "getprop"]:
            return ok("H12 Pro\n")
        if a[:2] == ["shell", "mkdir"]:
            return ok("")
        if a[0] == "push":
            self.pushed[a[2]] = open(a[1], "rb").read()
            return ok("1 file pushed")
        if a[:2] == ["shell", "ls"]:
            return ok("\n".join(self.files))
        if a[:2] == ["exec-out", "cat"]:
            name = a[2].strip("'").rsplit("/", 1)[1]
            return subprocess.CompletedProcess(cmd, 0, self.files[name].encode(), b"")
        if a[0] == "connect":
            return ok(f"connected to {a[1]}")
        return subprocess.CompletedProcess(cmd, 1, "", "unknown")


def test_remote_errors_say_what_to_do():
    none = RcLink(adb="adb", runner=FakeAdb())
    none.adb = None                                   # as on a computer without platform-tools
    with pytest.raises(RcError, match="platform-tools"):
        none.pick()
    with pytest.raises(RcError, match="Allow USB debugging"):
        RcLink(adb="adb", runner=FakeAdb("R5CT1 unauthorized\n")).pick()
    with pytest.raises(RcError, match="no remote connected"):
        RcLink(adb="adb", runner=FakeAdb("")).pick()
    rc = RcLink(adb="adb", runner=FakeAdb())
    assert rc.status()["model"] == "H12 Pro"
    assert rc.connect("192.168.1.50") == "192.168.1.50:5555"


def test_blocks_go_to_the_remote_and_records_come_back(app, herb):
    c, fid, op = herb
    rec = "start_time,flight_time_min,sprayed_area_ha,total_l\n2025-06-05T07:00:00,5,0.0400,0.80\n"
    fake = FakeAdb(files={"job_001.csv": rec, "notes.txt": "x"})
    app.state.rc.adb, app.state.rc.run = "adb", fake
    st = c.post(f"/api/farms/{fid}/stock-loop", json={"op_id": op, "tank_l": 1.0}).json()
    assert st["pushed"]["error"] is None and any(k.endswith("_block1.kml") for k in fake.pushed)
    out = c.post(f"/api/farms/{fid}/stock-loop/pull").json()
    assert out["added"] == 1 and out["state"]["blocks"][0]["records"][0]["source"] == "remote: job_001.csv"
    assert c.post(f"/api/farms/{fid}/stock-loop/pull").json()["added"] == 0           # each file once
    assert any(k.endswith("_block2.kml") or k.endswith("_block1.kml") for k in fake.pushed)


def test_no_remote_does_not_stop_the_job(app, herb):
    c, fid, op = herb
    app.state.rc.adb = None
    st = c.post(f"/api/farms/{fid}/stock-loop", json={"op_id": op, "tank_l": 1.0}).json()
    assert st["status"] == "running" and "platform-tools" in st["pushed"]["error"]
    assert c.post(f"/api/farms/{fid}/stock-loop/pull").status_code == 503
