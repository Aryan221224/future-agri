# 0017 — The Mission Console: one local app that runs the project's loop

**Status:** accepted · **Date:** 2026-11-01

## Context

The twin, the planner, the drone emulator and the scan model existed as Python
packages and command-line runs. The project needed software that the team and
farmers actually use: build a farm's digital twin from real data, look at it in
3D, rehearse the season many times, choose and approve a plan with timings, and
then run the season day by day with live data, approvals, flights and records.
It has to work on a field laptop with no login, be usable on a phone by a farmer,
and push heavy compute to the shared H200 server, whose password only the user
types.

## Decision

* **One local web app** (`services/console/fa_console`, FastAPI) serving a React
  single-page app (`apps/console`, built into `fa_console/static` and committed, so
  running it needs Python only). Bound to 127.0.0.1 by default; the UI and the
  API say so loudly if it is exposed. **No login** for now, by the user's choice.
* **SQLite + a persistent job queue.** Builds, iterations, long mission runs and
  weed detection are jobs with progress over SSE; they survive restarts as
  records. Local worker threads run them; a **remote worker** can claim jobs of
  kinds registered `remote=True`.
* **The season as a stepper** (`fa_runner.session.SeasonSession`): morning
  decisions, a pause for manual approvals, flights; every operator action is
  logged and replayed to rebuild state, with a data horizon so a replay never runs
  further than the original did. `run_season` is a wrapper; golden fingerprints
  prove the refactor kept results identical.
* **Virtual and live fields share one loop.** A virtual farm has hidden truth and
  is scored; a live field waits for weather, uploaded scans and recorded flights.
* **The GPU server through one SSH session.** `Connect-GPU-Server` pipes the
  worker code (and a one-time token file) over SSH, opens `-R 18099:127.0.0.1:8099`,
  and starts `python -m fa_console.worker`, in the NVIDIA PyTorch container when
  Docker has it. The worker runs the *same* job functions as the console on a
  downloaded copy of the farm, heartbeats every 60 s, and exits when the tunnel has
  been gone for two minutes (no hang-up signal reaches it without a terminal).
  No SSH keys are installed; the user types the password.
* **Weed detection** on an uploaded orthomosaic runs on the GPU worker with
  PyTorch FP32 (the reference, and fastest on the H200); ONNX and FP16 match it
  (IoU-matched recall 0.995), so the ONNX export is the input for a TensorRT engine
  built on the aircraft's computer. Detections are imported into the mission when
  the job finishes.
* **Scan realism from measurement.** The detector's recall and false positives per
  GSD come from the H200 sweep (2.5/5/10/20 mm), not from extrapolation.
* **English only; dark by default** with a Dark/Light switch in every header.
* **Exports for the K3-A Pro** (field and survey KML, the settings sheet), the plan
  (PDF, calendar, CSV) and records (CSV, bundle).

## Consequences

* The console is one process with one SQLite file per installation: simple to run
  and back up, not a multi-user server. A login and a shared database come with a
  multi-team deployment, not before.
* The worker needs the simulation stack on the server, including the forecast
  libraries: without them the iterations would fall back to climatology silently,
  so `requirements-worker.txt` pins them.
* The K3-A Pro still has no control port: the closed loop reaches it through files
  (KML, settings) and comes back through job records, a tracker, or an on-board
  computer. Live detection on the sprayer (camera + edge computer + TensorRT) is
  the next architecture step and needs its own decision.
