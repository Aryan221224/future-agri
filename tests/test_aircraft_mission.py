"""ArduPilot back-end pieces that need no simulator: missions, fence, links, parameters."""
import math

import pytest

import fa_aircraft  # noqa: F401  (MAVLink 2 before pymavlink)
from fa_aircraft.mission import M, fence_polygon, sortie_items
from fa_aircraft.runner import SAFETY_PARAMS, load_param_file
from fa_aircraft.vehicle import parse_link
from fa_agent.simrun import demo_job


def test_a_sortie_sprays_only_along_the_passes_and_ends_home():
    job = demo_job(n_passes=3)
    items = sortie_items(job, 0)
    cmds = [i.command for i in items]
    assert cmds[1] == M.MAV_CMD_NAV_TAKEOFF and cmds[-1] == M.MAV_CMD_NAV_RETURN_TO_LAUNCH
    on = [k for k, i in enumerate(items) if i.command == M.MAV_CMD_DO_SPRAYER and i.p1 == 1]
    off = [k for k, i in enumerate(items) if i.command == M.MAV_CMD_DO_SPRAYER and i.p1 == 0]
    assert len(on) == len(off) == 3 and all(a < b for a, b in zip(on, off))
    for a, b in zip(on, off):                                  # between on and off: pass waypoints at spray height
        wps = [i for i in items[a + 1:b] if i.command == M.MAV_CMD_NAV_WAYPOINT]
        assert wps and all(abs(w.alt - job.height_m) < 1e-9 for w in wps)


def test_the_fence_holds_the_field_and_the_take_off_point():
    from shapely.geometry import Point, Polygon
    job = demo_job()
    fence = Polygon(fence_polygon(job))
    assert fence.contains(Point(0, 0))
    assert fence.buffer(1e-6).contains(Polygon(job.fence))
    assert len(fence.exterior.coords) - 1 <= 60


def test_links():
    assert parse_link("serial:COM5:57600") == ("COM5", {"baud": 57600})
    assert parse_link("serial:/dev/ttyUSB0:115200") == ("/dev/ttyUSB0", {"baud": 115200})
    assert parse_link("udpin:0.0.0.0:14550")[0] == "udpin:0.0.0.0:14550"
    assert parse_link("tcp:192.168.4.1:5760")[0] == "tcp:192.168.4.1:5760"
    with pytest.raises(ValueError, match="unknown link"):
        parse_link("bluetooth:xx")


def test_the_airframe_file_sets_every_safety_parameter():
    p = load_param_file()
    assert set(SAFETY_PARAMS) <= set(p)
    assert p["FENCE_ENABLE"] == 1 and p["FS_GCS_ENABLE"] >= 1 and p["SPRAY_ENABLE"] == 1
    assert p["BATT_CRT_VOLT"] < p["BATT_LOW_VOLT"] < p["MOT_BAT_VOLT_MAX"]
    assert math.isclose(p["BATT_CRT_VOLT"] / 12, 3.45, abs_tol=0.01)
