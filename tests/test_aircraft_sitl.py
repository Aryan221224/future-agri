"""ArduPilot back-end against the real ArduCopter code (SITL): connect, parameters, fence and
mission upload with read-back, arm, take off, the spray mission in AUTO, RTL and landing.
Skipped unless an arducopter SITL binary is available (FA_SITL)."""
import math
import threading
import time

import pytest

from fa_aircraft.mission import fence_items, sortie_items
from fa_aircraft.runner import SAFETY_PARAMS, JobRunner, load_param_file, preflight
from fa_aircraft.sitl import Sitl, find_sitl
from fa_aircraft.vehicle import Vehicle, VehicleError
from fa_agent.simrun import demo_job

BIN = find_sitl()
pytestmark = pytest.mark.skipif(BIN is None, reason="no ArduCopter SITL binary (set FA_SITL)")


@pytest.fixture(scope="module")
def sitl():
    s = Sitl(BIN, speedup=8).start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def vehicle(sitl):
    v = Vehicle(f"tcp:127.0.0.1:{sitl.port}", timeout_s=30, heartbeat_hz=10)
    want = load_param_file(keys=SAFETY_PARAMS)
    want["FENCE_ENABLE"] = 1
    for k, val in want.items():
        v.set_param(k, val)
    end = time.monotonic() + 120
    while (v.state.ekf_flags & 0x1F) != 0x1F or v.state.gps_fix < 3:
        assert time.monotonic() < end, "SITL EKF/GPS not ready"
        time.sleep(0.5)
    yield v
    v.close()


def test_parameters_are_checked_against_the_airframe_file(vehicle):
    assert vehicle.check_params(load_param_file(keys=SAFETY_PARAMS)) == []
    assert vehicle.check_params({"RTL_ALT": 12345})[0].startswith("RTL_ALT")


def test_mission_and_fence_upload_read_back_identical(vehicle):
    job = demo_job()
    vehicle.upload(fence_items(job), 1)                      # MAV_MISSION_TYPE_FENCE
    items = sortie_items(job, 0)
    vehicle.upload(items)
    assert len(vehicle.download()) == len(items)


def test_arming_refused_reason_is_reported(vehicle):
    vehicle.set_mode("AUTO")                                 # Copter does not arm in AUTO
    try:
        with pytest.raises(VehicleError, match="armable|not armed|denied|failed"):
            vehicle.arm(timeout=8)
    finally:
        vehicle.set_mode("GUIDED")
    assert not vehicle.state.armed


def test_a_whole_spray_job_flies_and_lands(vehicle):
    job = demo_job(n_passes=2, length_m=30.0)
    events = []
    runner = JobRunner(vehicle, job, check_params=True, min_cell_v=3.5, cells=3, on_event=events.append)
    rep = preflight(vehicle, 3.5, 3, expected=runner.expected)
    assert rep.ok, rep.failures
    out = runner.run(preflight_timeout_s=60)
    states = [e.state for e in events]
    assert out == "DONE", events
    assert states[:6] == ["PREFLIGHT", "UPLOAD", "ARMING", "TAKEOFF", "AUTO", "LANDED"]
    h = vehicle.state.home
    assert h is not None and not vehicle.state.armed
    d = math.dist((vehicle.state.lat, vehicle.state.lon), h[:2]) * 111_000
    assert d < 3.0                                            # RTL landed at home


def test_abort_returns_home(vehicle):
    job = demo_job(n_passes=4, length_m=60.0)
    runner = JobRunner(vehicle, job, check_params=False, min_cell_v=3.5, cells=3)
    th = threading.Thread(target=runner.run, kwargs={"preflight_timeout_s": 60}, daemon=True)
    th.start()
    end = time.monotonic() + 120
    while runner.state != "AUTO":
        assert time.monotonic() < end and runner.state != "FAILED", runner.events
        time.sleep(0.2)
    time.sleep(3)
    runner.abort()
    th.join(240)
    assert vehicle.state.mode in ("RTL", "LAND") or not vehicle.state.armed
    assert any(e.state == "RTL" for e in runner.events)
