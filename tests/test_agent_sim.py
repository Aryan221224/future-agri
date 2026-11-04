"""fa-agent flying a simulated K3-A Pro through the S-BUS bridge's real C core, end to end,
with every failsafe injected."""
import math

import pytest

from fa_agent.pilot import AIRBORNE, Pilot
from fa_agent.route import Job
from fa_agent.simrun import demo_job, run
from fa_agent.sim import SimK3

pytestmark = pytest.mark.skipif(__import__("shutil").which("make") is None or
                                __import__("shutil").which("gcc") is None, reason="needs gcc to build the bridge core")


def _states(r):
    return [e.state for e in r.events]


def _no_throttle_low_in_the_air(monkeypatch):
    """Wraps Pilot.tick: the ground-idle throttle (full stick down = the fastest descent in
    GPS-SPEED) must never be sent in the air; commanded descents stay within the climb cap."""
    real = Pilot.tick
    bad = []

    def tick(self, now, fix, power, bridge):
        us = real(self, now, fix, power, bridge)
        if us is not None and self.state in AIRBORNE and self.state not in ("LAND", "LAND_NOW") \
                and us[self.profile.throttle.ch] <= 1050 and fix is not None and fix.h > 0.5:
            bad.append((now, self.state))
        return us
    monkeypatch.setattr(Pilot, "tick", tick)
    return bad


def test_a_whole_job_flies_accurately_and_sprays_only_on_the_passes(monkeypatch):
    bad = _no_throttle_low_in_the_air(monkeypatch)
    r = run(demo_job(), max_s=600)
    assert r.state == "DONE", r.summary()
    assert r.xtrack_rms < 0.35 and r.xtrack_p95 < 0.5            # uncalibrated, 3 m/s wind
    assert 0.85 * 4 * 60 / 5.0 <= r.spray_s <= 1.1 * 4 * 60 / 5.0  # all four passes sprayed
    assert all(state == "PASS" for _, _, _, _, state, pump in r.track if pump)
    assert not bad
    assert [e[1] for e in r.k3_events] == ["armed", "takeoff", "landed"]


def test_two_sorties_with_a_battery_swap():
    r = run(demo_job(n_passes=4, sorties=2), max_s=900)
    assert r.state == "DONE"
    assert [e[1] for e in r.k3_events].count("landed") == 2
    assert _states(r).count("PREFLIGHT") == 2


def test_agent_stall_mid_pass_hovers_then_the_k3_returns_home():
    r = run(demo_job(), max_s=400, faults=[(25.0, "agent", False)])
    assert r.bridge_sources.get("hold", 0) > 0 and r.bridge_sources.get("rth", 0) > 0
    assert [e[1] for e in r.k3_events][-2:] == ["rth", "landed"]
    stall = [p for p in r.track if p[0] >= 25.2]
    assert not any(pump for *_, pump in stall)


def test_rtk_float_pauses_then_resumes_the_pass_where_it_stopped():
    clean = run(demo_job(), max_s=600)
    r = run(demo_job(), max_s=600, faults=[(22.0, "rtk", "float"), (27.0, "rtk", "fixed")])
    assert r.state == "DONE"
    assert "HOLD" in _states(r)
    assert r.spray_s <= clean.spray_s * 1.1                        # resumed, not restarted


def test_rtk_float_that_does_not_recover_returns_home():
    r = run(demo_job(), max_s=600, faults=[(22.0, "rtk", "float")])
    st = _states(r)
    assert "HOLD" in st and "RETURN" in st and r.state in ("LANDED", "DONE")
    assert any("RTK float for" in e.reason for e in r.events)


def test_lost_heading_hands_over_to_the_k3_return_home():
    r = run(demo_job(), max_s=600, faults=[(22.0, "heading", False)])
    assert "RTH_K3" in _states(r)
    assert any(e[1] == "landed" for e in r.k3_events)


def test_pilot_takes_over_and_hands_back():
    r = run(demo_job(), max_s=700, faults=[(22.0, "auto_switch", False), (30.0, "auto_switch", True),
                                           (33.0, "resume", None)])
    st = _states(r)
    assert "PILOT" in st and "HOLD" in st and r.state == "DONE"
    assert r.bridge_sources.get("pilot", 0) > 100


def test_battery_low_returns_home():
    r = run(demo_job(), max_s=600, faults=[(25.0, "battery_used_ah", 14.5)])
    assert any("battery" in e.reason for e in r.events)
    assert "RETURN" in _states(r) or "LAND_NOW" in _states(r)
    assert any(e[1] == "landed" for e in r.k3_events)


def test_abort_returns_and_lands():
    r = run(demo_job(), max_s=600, faults=[(25.0, "abort", None)])
    st = _states(r)
    assert "RETURN" in st and r.state == "DONE" and r.spray_s < 30


def test_preflight_refuses_without_the_auto_switch():
    r = run(demo_job(), max_s=30, auto_switch=False)
    assert r.state == "IDLE" and any("AUTO switch" in e.reason for e in r.events)
    assert not r.k3_events


def test_strong_wind_still_on_the_line():
    r = run(demo_job(), max_s=600, k3=SimK3(wind_ms=8.0, seed=3))
    assert r.state == "DONE" and r.xtrack_p95 < 0.6


def test_pilot_receiver_lost_at_auto_keeps_flying():
    r = run(demo_job(), max_s=600, faults=[(20.0, "pilot_rx", False)])
    assert r.state == "DONE"


def test_a_job_outside_its_fence_is_refused():
    j = demo_job()
    raw = {"schema": "fa.job/1", "job_id": "x", "home": {"lat": 28.47, "lon": 77.50},
           "fence": [list(j.local.to_lonlat(*p)) for p in j.fence],
           "sorties": [[{"points": [list(j.local.to_lonlat(10, 10)), list(j.local.to_lonlat(300, 10))]}]]}
    with pytest.raises(ValueError, match="fence"):
        Job.from_json(raw)
    raw["sorties"] = [[{"points": [list(j.local.to_lonlat(10, 10)), list(j.local.to_lonlat(10, 50))]}]]
    job = Job.from_json(raw)
    assert math.isclose(job.sorties[0][0].length_m, 40.0, abs_tol=0.01)


def test_landed_by_the_pilot_the_agent_stops_flying():
    """The pilot takes over and lands (throttle down): the agent sees motors stopped on the
    ground and ends in LANDED, never 'holding' or 'returning' a parked drone."""
    r = run(demo_job(), max_s=200, faults=[(22.0, "auto_switch", False), (22.1, "pilot_throttle", 1000),
                                           (40.0, "auto_switch", True)])
    st = _states(r)
    assert "PILOT" in st and r.events[-1].state in ("LANDED", "DONE")
    assert "RETURN" not in st[st.index("PILOT"):]


def test_python_frames_are_what_the_firmware_parses():
    import ctypes

    from fa_agent.bridgelink import BridgeConfig, CoreBridge, FrameParser, channels_frame, encode, STATUS
    cb = CoreBridge()
    lib = cb.lib
    parser = ctypes.create_string_buffer(256)
    lib.link_parser_init(parser)
    msg = ctypes.create_string_buffer(80)
    us = [1500] * 16
    us[0], us[7] = 1777, 2000
    wire = channels_frame(us, 200, 9)
    got = sum(lib.link_parser_feed(parser, ctypes.c_uint8(b), msg) for b in wire)
    assert got == 1 and msg.raw[0] == 0x01 and msg.raw[1] == 36
    assert lib.bridge_on_link(cb._state, msg, ctypes.c_uint32(5))
    cb.pilot([2000 if i == 7 else 1500 for i in range(16)], 5)
    src, out = cb.output(6)
    assert src == "companion" and out[0] == 1777
    st = cb.status(7)
    assert st.source == "companion" and st.last_seq == 9 and st.auto_allowed
    # the config the agent sends is the layout the firmware unpacks
    cfg = BridgeConfig(hold_ms=1500, auto_ch=8)
    c = ctypes.create_string_buffer(64)
    assert lib.bridge_config_unpack(cfg.pack(), len(cfg.pack()), c)
    # and status frames from the firmware decode in Python
    p = FrameParser()
    frames = p.feed(b"\x00\x13" + encode(STATUS, bytes(18)) + b"\xa5\x01")
    assert frames == [(STATUS, bytes(18))]


def test_calibration_flight_measures_this_aircraft_and_improves_accuracy():
    from fa_agent.profile import K3Profile
    from fa_agent.sim import _true_profile
    true = _true_profile()
    prof = K3Profile.load()
    cal = run(demo_job(), max_s=400, profile=prof, calibrate=True, k3=SimK3(seed=5, wind_ms=2.0))
    assert cal.state == "DONE", cal.summary()
    rep = cal.pilot.calibration
    assert set(rep["applied"]) == {"pitch", "roll", "yaw"}, rep
    assert prof.pitch.gain == pytest.approx(true.pitch.gain, rel=0.1)
    assert prof.roll.gain == pytest.approx(true.roll.gain, rel=0.1)
    assert prof.pitch.dead == pytest.approx(true.pitch.dead, abs=0.03)
    assert prof.calibrated
    before = run(demo_job(), max_s=600, k3=SimK3(seed=6))
    after = run(demo_job(), max_s=600, profile=prof, k3=SimK3(seed=6))
    assert after.state == "DONE" and after.xtrack_rms <= before.xtrack_rms
