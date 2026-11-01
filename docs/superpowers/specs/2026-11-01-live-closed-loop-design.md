# Live closed loop: the sprayer sees, the twin corrects the run in progress

**Date:** 2026-11-01 · **Status:** draft for review

## What the user asked for

> The camera will be on the spraying drone, and it will be a closed-loop system
> reporting live how the data currently is. The on-board computer can be any
> computer drones use for this task; the camera can be any camera drones use for
> this task. The simulation sees the deviation from its best-path predictions and
> makes the current run even better with all the data.

Understanding (assumptions marked *A*):

* One aircraft carries the spray system **and** a camera with an on-board
  (companion) computer. There is no separate survey drone in this mode.
* Hardware-agnostic: NVIDIA Jetson (Orin Nano/NX/AGX), x86 mini-PCs with an NVIDIA
  GPU, or CPU-only boards. Cameras over USB/UVC, CSI, or an RTSP/GigE stream.
  NPU boards (Hailo, RK3588) run the ONNX path through a vendor compiler; that
  compile step is an adapter, not part of this build (*A*).
* "Best-path prediction" = everything the plan predicted *before* the run: per-cell
  crop state (weeds, canopy, blight), the route, the dose each cell receives, sortie
  times, battery and tank use.
* "Make the current run better" = while the job is still being flown, correct what
  remains of it, on three timescales (below). The K3-A Pro takes no commands, so on
  that aircraft corrections reach it as a new sortie file loaded by the operator;
  on an ArduPilot/PX4 sprayer they can go over MAVLink; with a boom valve on the
  companion computer, spot spraying happens in the same pass (*A*).

Success: on simulated farms, closed-loop runs use less product and leave fewer
weeds/blight than the open-loop plan, with the margin measured, not asserted; the
console shows the drone, what it sees, the deviation from prediction and the
corrected remaining plan live; the on-board agent runs unchanged on any supported
computer, and replays a recorded flight for testing.

## Three loops

| Loop | Period | Input | Correction |
|---|---|---|---|
| **In-pass** | ~100 ms | detections ahead of the boom | open/close the spot-spray valve (only with a valve adapter); never adds product beyond the plan's per-cell maximum |
| **Between sorties** | each battery swap (8–15 min) | everything seen and sprayed so far | re-plan the *remaining* job: which cells still need product, at what dose, re-fly skipped strips, route the next sortie, adjust height/speed/spacing if the observed track or dose deviates |
| **After the job** | end of mission | the whole run | full twin update, season re-plan (next operations, scans), iterations re-ranked |

## Deviation, not just data

Before the run the console freezes a **prediction** per cell and per sortie:

* crop: expected weed density, canopy cover and blight flag, each with its spread
  from the twin's ensemble;
* flight: planned swaths, planned dose per cell, sortie time, battery and tank use.

As data arrives, per channel:

* **crop deviation** = observed − predicted, as a z-score using the twin spread and
  the observation error at the flight's GSD (recall and false positives from the H200
  sweep calibration). Only |z| > 2 over enough cells counts as a deviation, so
  noise is not chased;
* **track deviation** = cross-track error and skipped strips from GNSS vs the swaths;
* **dose deviation** = as-applied (flow × track, through the deposition model) vs
  prescribed, per cell.

Each significant deviation has a named correction, and the console shows which
one was applied and why (the audit log records it).

## Components

### 1. Live run in the console (`fa_console/live_run.py`, routes `/api/live`)

* `LiveRun` for one operation of a mission (a spray job): created when the job
  starts; holds the frozen prediction, the farm grid, the remaining plan, and
  running per-cell state: observed (with an **observation mask** — cells the
  camera footprint covered), sprayed dose, deviation flags.
* Ingest: telemetry batches and per-cell observations from the on-board agent
  (device-token auth, reusing `DeviceService`), idempotent by sequence number so
  store-and-forward replays are safe.
* Assimilation: per-cell observations go through the scan importer's observation
  operator with the coverage mask (unobserved cells keep the prior — today an
  empty cell reads as "no weeds", which partial coverage must not do), then into
  the session as a partial scan.
* Between-sortie re-plan: the planner's existing rules re-run on the updated maps
  (herbicide: none / blanket / spot from the weed map; fungicide: the blight
  decision) give the target cells; minus cells already dosed ≥ 90 % of target,
  plus skipped strips; `generate_route` and
  `simulate_mission` for the remainder from the current battery and tank. Outputs:
  KML for the K3-A Pro (target polygons), a MAVLink mission for capable aircraft,
  and the settings sheet if height/speed/spacing changed.
* Live view: the 3D farm shows the drone, its camera footprint, detections as they
  arrive, a deviation layer, and the remaining plan; the Mission page shows the
  sortie log and the corrections with their reasons.

### 2. On-board agent (`services/onboard/fa_onboard`)

Runs on the companion computer; Python, no console code.

* **Sources** (adapters): camera — OpenCV/GStreamer capture (UVC, CSI, RTSP);
  position/attitude — MAVLink from the flight controller when it has a telemetry
  port, else a GNSS receiver (NMEA/UBX) plus the camera's own IMU if any; spray
  state — FC telemetry or a flow sensor.
* **Inference**: `fa_gateway.rfdetr` backends — TensorRT FP16 on NVIDIA (engine
  built on the device from the verified ONNX export: `fa_onboard build-engine`),
  ONNX Runtime on CPU/other, PyTorch for bench tests. Detections through the
  existing conformal action gate; unsure ones become review items (thumbnail only).
* **Georeferencing**: pinhole camera model (intrinsics from a calibration file),
  height above ground from the FC or the farm DEM, attitude, GNSS lever arm →
  frame footprint and detection points in WGS84.
* **Per-cell aggregation** on board with the farm grid downloaded at job start, so
  the uplink carries cell summaries (tens of bytes per cell), never imagery.
* **Store-and-forward** queue on disk; the link (4G, Wi-Fi to the field laptop)
  may drop for minutes.
* **Actuation** (adapters, all off by default): `report` (K3-A Pro), `mavlink`
  (ArduPilot/PX4: spray servo/relay and next-sortie mission upload, each upload
  confirmed by the operator on the console), `valve` (GPIO/PWM boom valve for
  in-pass spot spraying, look-ahead geometry checked against the measured
  detection + valve latency).
* **Replay mode**: feeds a recorded frame+telemetry log (or the simulator's) through
  the same code, for tests and post-flight analysis.

### 3. The sprayer camera in simulation (`fa_scan.sprayer_camera`)

The virtual farm gets a camera on the virtual sprayer: along the simulated track it
produces the same per-cell messages the agent sends, from the hidden truth with the
measured detector recall/false positives at the flight GSD and footprint coverage.
This is what makes the whole loop testable and benchmarkable without hardware, and
it is the same code path the console uses for real data.

## Benchmark (the measured claim)

Open loop (today's plan) vs closed loop (three timescales; with and without the
valve), template farms × seeds, on the H200 CPUs: product used, weeds/blight left,
yield and net per ha, coverage error, time. Reported with spread across seeds, in
an ADR, before anyone quotes a number.

## Safety and rules

* The agent never commands flight directly; mission changes go through the FC's own
  mission interface and only after operator confirmation on the console.
* Spot spraying can only reduce product relative to the approved plan; buffers and
  no-spray zones from the region pack are enforced on board and in the re-plan.
* The operator can freeze the plan (open loop) at any time; every correction is in
  the audit log with its reason.

## Out of scope for this build

Hardware selection and mounting, certification, NPU vendor compilers, a valve
controller's electronics, and in-flight changes on the K3-A Pro itself (it has no
control port; it gets the next sortie's file).

## Testing

* Unit: georeferencing (synthetic camera at known pose → known ground points),
  observation mask and partial-scan assimilation, deviation z-scores, remaining-job
  re-plan (sprayed cells drop out, skipped strips come back), message schema and
  idempotent ingest, look-ahead feasibility.
* Integration: simulated sprayer camera → agent replay mode → console live run →
  corrected next sortie, end to end on a virtual farm.
* Browser: live view shows drone, footprint, detections, deviation layer and the
  corrected plan.
* Hardware: TensorRT engine build and per-frame latency measured on whatever
  companion computer is used (the H200 engine is only for parity/timing reference).

## Order of work

1. Partial-scan assimilation with an observation mask; the frozen prediction and
   deviation scoring.
2. Simulated sprayer camera; `LiveRun` with ingest and between-sortie re-plan;
   live view.
3. On-board agent: sources, inference, georeferencing, aggregation, store-and-
   forward, replay mode, engine build.
4. Actuation adapters (report, mavlink, valve) with simulated back-ends.
5. Benchmark on the H200 and ADR 0018 with the measured result.
