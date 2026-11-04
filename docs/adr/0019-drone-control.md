# 0019: The software flies the drone (K3-A Pro virtual pilot, stock loop, ArduPilot)

**Status:** accepted (tested in simulation; first hardware run is the bench test) ·
**Date:** 2026-11-04 · **Design:** [spec](../superpowers/specs/2026-11-04-drone-control-design.md) ·
**Install:** [K3-A Pro autonomy kit](../hardware/k3a-autonomy-kit.md), [first flights](../hardware/first-flight.md)

## Context

The user's drone is a 10 L quad on a **JIYI K3-A Pro**, which is closed: it has no SDK, no
MAVLink and no published protocol, and it cannot run other firmware. The user wants the
software to run the drone end to end, over a wire or wirelessly, both with hardware added
and with the drone as it is today, and with an ArduPilot flight controller as well.

## Decision

Three ways to fly, one job format (`fa.job/1`). The console builds the job from the drone
twin's own passes over the job's targets, split into tank-sized sorties, with the field as
the fence.

1. **K3-A Pro with the autonomy kit: a virtual pilot.** The K3-A Pro reads an S-BUS receiver
   and has a GPS-SPEED mode, in which the sticks command speed. An RP2040 bridge sits between
   the pilot's receiver and the K3-A Pro and writes the stick channels for `fa-agent` on a
   Raspberry Pi. The agent closes the loop on its own RTK pair (position and heading).
   * Safety is in hardware first:
     * A transmitter AUTO switch decides who flies.
     * The fail-over mux's select line is pulled down, so the pilot is the default.
     * A watchdog reset hands control straight to the pilot.
     * A stale companion frame makes the bridge hold, then use JIYI's return home.
     * The bridge never re-configures in flight.
   * The agent's failsafes:
     * RTK float: hold.
     * RTK lost or no heading: hand over to JIYI's return home.
     * Leaving the geofence or going above the ceiling: return.
     * Low battery: return. Critical battery: land.
     * Off the line: hold, then return.
     * The pilot takes over: hands off. When the pilot hands back: hold.
     * Landed by someone else: stop.
     * Airborne with no job: hold position.
     * The ground-idle throttle is only ever sent on the ground.
   * A calibration flight measures this aircraft's stick response, and a guided props-off
     bench test verifies every assumption before the first flight.
2. **Stock K3-A Pro: the loop closes between sorties.**
   * The job is planned in tank-sized blocks, one KML each for Agri Assistant.
   * Each block's JIYI flight record is checked against its plan. A partial block is flown
     again, using JIYI's breakpoint resume. An overdose is flagged.
   * The twin re-plans what is left with its own rule, and checks the spray window.
   * The finished job is booked in the twin as one replayable action (`book_live_run`).
   * The Android remote can be linked over USB or Wi-Fi ADB. KMLs are pushed to it and
     records are pulled back. The console never taps the app.
3. **ArduPilot over MAVLink 2:**
   * Any link: USB, SiK radio, UDP or TCP over Wi-Fi or 4G.
   * The safety parameters are checked against the airframe file.
   * The preflight gate reports ArduPilot's own PreArm and arming reasons.
   * Fence and mission are uploaded and read back as ArduPilot stores them.
   * The flight: GUIDED arm and take-off, then the AUTO mission with the sprayer switched at
     every pass, RTL, battery swap, next sortie.
   * ArduPilot's failsafes stay in charge in the air, and the runner never fights them.
   * The fence joins the field to the take-off point, because ArduPilot refuses to arm
     outside its fence.

## Measured (simulation and SITL)

* **Bridge core:** 51 host checks pass under ASan and UBSan. The same C file is loaded into
  the Python simulator, so the logic flown in tests is the firmware's.
* **K3-A Pro virtual pilot on the simulated K3-A Pro:** 17 end-to-end tests cover every
  failsafe above.
  * The simulated aircraft's true stick gains differ from the defaults on purpose.

  | Wind | Profile | Cross-track RMS | 95th percentile |
  |---|---|---|---|
  | 3 m/s | default (uncalibrated) | 0.19–0.21 m | 0.30–0.34 m |
  | 3 m/s | calibrated | 0.18 m | 0.33 m |
  | 8 m/s | default | 0.36 m | 0.54 m |
  | 8 m/s | calibrated | 0.31 m | 0.47 m |

  * The calibration flight recovers the true gains within 2–9 %.
* **ArduPilot:** against ArduCopter 4.5.7 SITL, a whole job flies from the console route on
  the farm's own field. Telemetry reaches the live run, the aircraft lands within 3 m of
  home, and abort leads to RTL.
* **Console → agent:** the console builds the job, the real agent picks it up over its link
  and flies it on the simulated K3-A Pro, and its state comes back to the console.

## Limits, said plainly

* None of this has flown on the real aircraft yet. The simulated K3-A Pro is a model built
  from its manual: first-order speed response, lag and wander. The real response, the CH5
  value for GPS-SPEED, the arming combination and the pump channel are found by the bench
  test and the calibration flight, not assumed.
* `firmware/sbus_bridge/src/main.c` was type-checked against the Pico SDK 2.1.1 headers but
  not compiled with the Arm toolchain here: downloading the toolchain binary was not
  permitted. The first build is on the user's machine or in CI.
* Spray height in GPS-SPEED is held on RTK height above take-off. Terrain following relies on
  the K3-A Pro's radar.
* The stock loop knows each block's mean dose, not a per-cell trace.
* Adding a control device to a certified drone may void its warranty or type certificate.

## Consequences

* The next step on hardware is `fa_agent check`, then `bench`, then a hover hand-over, then
  calibration ([first-flight.md](../hardware/first-flight.md)). Measured accuracy replaces the
  simulated numbers in this ADR.
* An ArduPilot swap gives the most direct control and is the path for fleets. The kit keeps the
  K3-A Pro and its certification paperwork. The stock loop needs no hardware at all.
