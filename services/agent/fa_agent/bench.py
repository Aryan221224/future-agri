"""Bench test on the real aircraft, PROPELLERS OFF: checks every assumption the virtual pilot
makes about this K3-A Pro before the first flight, and writes what it found to the profile.

    python -m fa_agent check --config agent.yaml    # sensors only, no outputs
    python -m fa_agent bench --config agent.yaml    # guided: asks you to confirm each step

Each step prints what to look for (in JIYI Assistant2 on a laptop connected to the K3-A Pro's
LINK port, or on the aircraft) and waits for y / n. Nothing is armed until you type ARM.
"""
from __future__ import annotations

import time

from .profile import K3Profile


def _ask(q: str) -> bool:
    while True:
        a = input(f"{q} [y/n] ").strip().lower()
        if a in ("y", "n"):
            return a == "y"


def _send_for(bridge, us, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        bridge.send(us, 200)
        bridge.poll()
        time.sleep(0.05)


def check(cfg) -> int:
    """Read the bridge, both RTK receivers, the power monitor and the console once."""
    from .main import Agent
    ok = True
    a = Agent(cfg)
    a.front.start()
    if a.rear:
        a.rear.start()
    if a.ntrip:
        a.ntrip.start()
    time.sleep(3.0)
    st = a.bridge.poll()
    print("bridge:", "no status (wiring? firmware flashed?)" if st is None else
          f"source={st.source} pilot_ok={st.pilot_ok} auto={st.auto_allowed} pilot frames {st.pilot_good}"
          f"/{st.pilot_bad} bad, link errors {st.link_errors}")
    ok &= st is not None and st.pilot_ok
    f = a.fixes.fix(time.monotonic())
    print("rtk:", "no fix" if f is None else f"{f.kind}, {f.sats} sats, heading "
          f"{'%.1f' % f.heading if f.heading_ok else 'NOT valid'}")
    ok &= f is not None and f.kind == "fixed" and f.heading_ok
    if a.ntrip:
        print("ntrip:", f"{a.ntrip.bytes_in} bytes", a.ntrip.error or "")
    p = a.power.read() if a.power else None
    print("power:", "no monitor" if p is None else f"{p.voltage:.2f} V ({p.voltage / 12:.2f} V/cell), {p.current:.1f} A")
    ok &= p is not None
    online = a.link.state(a.snapshot())
    print("console:", "reachable" if online else f"NOT reachable ({a.link.error})")
    ok &= online
    print("CHECK", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def bench(cfg) -> int:
    from .bridgelink import SerialBridge
    print("BENCH TEST - REMOVE ALL PROPELLERS NOW. Tank empty or water only. Pilot transmitter ON.")
    if not _ask("Are all four propellers off?"):
        return 1
    prof = K3Profile.load(cfg.profile)
    br = SerialBridge(cfg.bridge_port)
    results = {}
    time.sleep(1.0)
    st = br.poll()
    results["bridge"] = st is not None
    print("1. bridge status:", st)
    print("2. Flip the AUTO switch (CH%d) on the transmitter to AUTO." % (prof.auto_ch + 1))
    input("   press Enter when done ")
    st = br.poll()
    results["auto_switch"] = bool(st and st.auto_allowed)
    print("   AUTO seen by the bridge:", results["auto_switch"])
    neutral = prof.neutral()
    neutral[prof.throttle.ch] = 1000
    _send_for(br, neutral, 2.0)
    results["mode"] = _ask(f"3. Assistant2 (or the K3-A Pro LED) shows GPS-SPEED mode with CH5 = {prof.mode_gps_us} us?")
    if not results["mode"]:
        for v in (1100, 1300, 1500, 1700, 1900):
            us = list(neutral)
            us[prof.mode_ch] = v
            _send_for(br, us, 2.0)
            if _ask(f"   with CH5 = {v} us, is it GPS-SPEED?"):
                prof.mode_gps_us, results["mode"] = v, True
                break
    for name, ax in (("roll right", prof.roll), ("pitch forward", prof.pitch), ("yaw clockwise", prof.yaw)):
        us = list(neutral)
        us[ax.ch] = 1500 + 300 * ax.sign
        _send_for(br, us, 2.0)
        if not _ask(f"4. Assistant2's RC monitor shows {name} (CH{ax.ch + 1} moving the right way)?"):
            ax.sign = -ax.sign
            print("   direction reversed for", name)
    if input("5. Type ARM to try the arming stick sequence (motors will spin without props): ").strip() == "ARM":
        for ms, chs in prof.arm_sequence:
            us = list(neutral)
            for c, v in chs.items():
                us[int(c)] = int(v)
            _send_for(br, us, ms / 1000.0)
        _send_for(br, neutral, 1.0)
        results["arm"] = _ask("   did the motors start spinning?")
        _send_for(br, neutral, 4.0)        # throttle low: the K3-A Pro disarms by itself after 3 s
        results["auto_disarm"] = _ask("   did they stop by themselves after about 3 s?")
    us = list(neutral)
    us[prof.pump_ch] = prof.pump_on_us
    _send_for(br, us, 2.0)
    results["pump"] = _ask(f"6. Did the pump run with CH{prof.pump_ch + 1} = {prof.pump_on_us} us (manual pump mode)?")
    _send_for(br, neutral, 0.5)
    print("7. Fail-over: stop sending for 3 s (the bridge holds then sends return home).")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 3.0:
        br.poll()
        time.sleep(0.05)
    st = br.poll()
    results["failover"] = bool(st and st.source in ("hold", "rth"))
    print("   bridge source now:", st.source if st else None)
    print("8. Flip the AUTO switch back to manual.")
    input("   press Enter when done ")
    st = br.poll()
    results["manual"] = bool(st and st.source == "pilot")
    out = cfg.profile or "k3a_pro.bench.yaml"
    prof.save(out)
    print("\nRESULTS:", results)
    print("profile written to", out)
    ok = all(results.values())
    print("BENCH", "PASSED: ready for the hover test (docs/hardware/first-flight.md)" if ok else "FAILED: fix before flying")
    return 0 if ok else 1
