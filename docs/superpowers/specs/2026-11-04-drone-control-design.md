# Drone control: the software flies the drone

**Status:** approved in conversation 2026-11-04 ("start building everything ... both ArduPilot and K3").
**Follows:** [live closed loop](2026-11-01-live-closed-loop-design.md) (its "plan 2": on-board agent and actuation).

## Intent (what the user asked for)

* The Future Agri software runs the real drone end to end: arm, take off, fly the planned
  passes, spray, return, land, sortie after sortie, re-planning from what the camera saw.
* Over a wire and wirelessly; whichever link is there.
* For the drone we have today (10 L quad, **JIYI K3-A Pro** flight controller), adding the
  hardware needed, as accurately as possible; and for an **ArduPilot** flight controller.
* The software does everything; the pilot watches and can always take over with the RC.
* It should work the first time it is connected, with every failure we can foresee handled.

## Facts that shape the design

* The K3-A Pro is closed: no SDK, no MAVLink, no telemetry protocol, no third-party firmware.
* Its manual documents an **S-BUS (or PPM) receiver input** and a **GPS-SPEED mode** in which
  CH1/CH2 command roll/pitch *speed*, CH3 climb rate, CH4 yaw rate, CH5 the flight mode and
  CH6 one-key return home; RC-signal loss with a GPS fix triggers its own return home.
  Arming is a stick combination (shown only as figures in the manual).
* So the K3-A Pro can be flown by a **virtual pilot**: a board between the RC receiver and the
  flight controller that writes the S-BUS frames itself, driven by our own RTK position.
* ArduPilot Copter speaks MAVLink, has a built-in sprayer (SPRAY_*), missions, geofence and
  failsafes, and a simulator (SITL) we can test against.

"First try working" is a goal, not a guarantee: nothing that flies is proven before it flies.
The design gets as close as possible by testing every layer in simulation, then a props-off
bench test that checks every assumption about the real aircraft, then staged first flights.

## Two vehicle back-ends, one console

```
Mission Console ── aircraft service ──┬── ArduPilot back-end: MAVLink over USB / serial radio /
   (plans, live run, Aircraft page)   │     UDP / TCP (Wi-Fi, 4G via the companion's router)
                                      └── K3 back-end: commands to fa-agent (outbound poll from
                                            the drone, works through 4G NAT) ── fa.live/1 back
Drone (K3):  pilot RX ─S-BUS─▶ bridge MCU ─S-BUS─▶ K3-A Pro (GPS-SPEED)
                               ▲ serial                 relay: bridge dead → pilot RX direct
                    fa-agent (Pi) ◀─ RTK F9P x2 (position + heading), camera, power monitor
Drone (ArduPilot): FC ─UART─▶ companion (mavlink-router + fa-agent camera/uplink)
                   FC ─TELEM1─▶ SiK radio ─▶ laptop USB;  FC USB ─▶ laptop (bench)
```

## K3-A Pro virtual pilot

**Added hardware** (BOM in `docs/hardware/k3a-autonomy-kit.md`): Raspberry Pi 5 (or Jetson
Orin Nano), RP2040 S-BUS bridge board with a fail-over relay, u-blox ZED-F9P ×2 (moving base:
position + heading), RTK corrections from an own F9P base or NTRIP over 4G, the official JIYI
RTK module and height radar on the K3-A Pro, INA228 power monitor, pump relay, 4G modem, SiK
radio (optional), nadir camera.

**Bridge firmware** (`firmware/sbus_bridge`, RP2040, C): one portable core (`bridge_core.c`)
used by the firmware, the host unit tests and the Python simulator (via a shared library), so
the logic flown is the logic tested.

* Reads the pilot's S-BUS, reads companion frames (`0xA5` framing, CRC-16/CCITT), writes S-BUS
  to the K3-A Pro every 14 ms. RP2040 UARTs invert in hardware: no external inverters.
* Source selection, in order: pilot override switch (a transmitter switch channel) → pilot;
  fresh companion frame (TTL ≤ 250 ms) → companion; companion stale → **hold** (sticks centre,
  GPS-SPEED: the K3-A Pro hovers) for 2 s, then **return home** (CH6) while still holding the
  sticks centre; pilot receiver failsafe flag while on pilot → pass through (K3-A Pro's own
  RC-loss return home).
* Relay: energised only while the core is healthy; firmware hang → hardware watchdog reset →
  relay drops → pilot receiver connected straight to the K3-A Pro.
* Status frames back to the companion: source, pilot switch, pilot RX ok, frame counters.

**fa-agent** (`services/agent/fa_agent`, Python on the companion):

* GNSS: UBX NAV-PVT and NAV-RELPOSNED parsing; RTCM forwarded from NTRIP or the base.
* Stick map: speed = gain × deflection outside a dead band, per axis; gains fitted by a
  **calibration flight** (step inputs, measured response); defaults from the K3-A Pro's
  speed gears until calibrated.
* Path follower: along-track speed with cross-track correction (PI on cross-track error, feed
  forward on the pass direction), yaw held along the pass, altitude by climb-rate command.
* Flight state machine: disarmed → preflight → arm (configurable stick sequence) → take off →
  transit → passes (pump on only on spray segments) → return → land → wait battery →
  next sortie. Abort → return home. Every transition logged.
* Failsafes: RTK fix lost (hold, then return home after a timeout); geofence (field polygon
  plus margin) breach → return home; battery low (INA228) → return home; console link loss →
  finish the sortie and return (configurable); agent exception → stop writing frames (the
  bridge holds, then returns home).
* Spray: pump channel through the K3-A Pro (manual pump mode) and an independent hard cut
  relay on the 24 V pump line.
* Uplink: fa.live/1 batches (existing schema) with store-and-forward; commands from the
  console by outbound long poll with the device token.
* **Simulated K3-A Pro** (`sim_k3.py`): GPS-SPEED dynamics (first-order speed response, lag,
  wind, GNSS noise), arming by stick sequence, CH5/CH6, S-BUS-loss return home. The agent flies
  it end to end in tests through the real bridge core.

## ArduPilot back-end

`services/aircraft/fa_aircraft` (pymavlink):

* Links: `serial:COM5:57600`, `udp:0.0.0.0:14550`, `udpout:host:14550`, `tcp:host:5760`;
  heartbeat watch, reconnect, link-quality report.
* Vehicle: parameter download and check against the airframe file, preflight gate (EKF,
  GPS fix and satellites, battery, RC present, fence loaded, arming checks), mission and fence
  upload (MISSION_COUNT / REQUEST_INT / ITEM_INT / ACK, retries, verify by download), mode,
  arm, take off, AUTO, RTL, land, sprayer (DO_SPRAYER in the mission at pass starts/ends).
* Missions from the drone twin's route: take off, passes at the planned speed with sprayer
  on/off, return; fence from the field polygon plus margin.
* Airframe parameters (`params/t1_10l_quad.param`): quad X, 12S, 34.7" props, sprayer on a
  servo/relay output, failsafes (RC, GCS, battery, EKF, fence), RTL altitude.
* Tested against an in-process fake vehicle; end to end against ArduPilot SITL when it can be
  built (CI workflow provided).

## Console

An **Aircraft** page per farm: choose the back-end and link, connect, live status (position,
mode, battery, GNSS, link), preflight checklist with pass/fail and reasons, **Start job**
(one click for the whole job), **Abort → return home**, sortie log. Telemetry feeds the
existing Live run.

## Testing and acceptance

1. Host unit tests: S-BUS codec, frame protocol, bridge mux and fail-over timings.
2. Agent end-to-end on the simulated K3-A Pro through the real bridge core: a full job flown;
   cross-track error reported; every failsafe injected (companion stall, RTK loss, geofence,
   battery, pilot override).
3. ArduPilot: fake vehicle protocol tests; SITL job end to end in CI.
4. Bench test on the real drone (props off) with a guided script; staged first flights
   (documented checklist). Measured accuracy replaces estimates.

## Out of scope / limits

* No reverse engineering of JIYI's closed data link.
* Accuracy on the K3-A Pro is bounded by how it responds to stick commands; the calibration
  flight measures it (estimate 0.2–0.5 m cross-track with RTK, not yet measured).
* Regulation: adding a control device to a certified drone may void its warranty or type
  certificate; the operator checks local rules (e.g. DGCA) before commercial use.
