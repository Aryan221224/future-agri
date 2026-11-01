# Future Agri Mission Console — operator guide

The console is the software the project runs on: it builds a field's digital twin
from real data, shows it in 3D, rehearses the season to choose a plan, and then runs
the season day by day — decisions, approvals, K3-A Pro flights, scans, records —
with live weather and devices. Farmers get a one-screen phone view.

## Start it

| Where | How |
|---|---|
| Windows laptop | double-click `scripts\console\Start-FutureAgri.cmd` |
| Linux / macOS | `scripts/console/start.sh` |
| Docker | `docker compose --profile console up --build console` |
| Developer | `pip install -r services/console/requirements.txt` then `make console` |

The first start creates `.venv` and installs packages (a few minutes, internet
needed). The console opens at **http://127.0.0.1:8099** and is reachable from this
computer only; it has **no login**. Data lives in `%USERPROFILE%\.futureagri\console`
(`~/.futureagri/console`): back that folder up. If a `data_cache` folder sits next
to the code, the console uses it, so template farms build offline.

## The loop, page by page

1. **Farms → Add a farm.** Pick the field on the map (or upload a GeoJSON/KML
   boundary, or let the console find the field outline), the crop template, the
   season and whether it is a **virtual test farm** (hidden truth, scored) or a
   **live field** (real weather; waits for your scans and flight records). The
   build fetches boundary, soil, terrain, weather and Sentinel-2 imagery.
2. **3D farm.** The twin in 3D: terrain, zones, canopy, weeds and blight as the
   season goes, Sentinel-2 imagery draped, any cell clickable.
3. **Drone twin.** The K3-A Pro model with its spec; fly a mission over the field
   and play it back with battery, tank and flow gauges.
4. **Iterations.** The twin rehearses the whole season many times with the
   forecasts that were really issued and ranks plans and K3-A Pro settings. Choose
   *This computer* or the *GPU server*. A live field rehearses on its own previous
   season.
5. **Plan.** The chosen plan with expected operations, dates, drone and crew time,
   batteries, litres and money; **Approve this plan** makes the mission run it.
   Download the field KML and the settings sheet for Agri Assistant, the survey
   KML for the camera flight, and the plan as PDF, calendar or CSV.
6. **Mission.** Day by day: the twin proposes sprays and scans, you approve, defer
   or reject, then fly (virtual) or record the real flight (live). On a live field,
   **Upload a scan** takes detector points (GeoJSON) or an orthomosaic (GeoTIFF) for
   weed detection on the GPU server.
7. **Season.** Charts of the crop the twin sees (and the hidden truth on virtual
   farms), weather and every flight and scan.
8. **Live.** Alerts, the spray outlook (share of forecast members in which flying is
   allowed), observed weather, the ensemble forecast, Sentinel-2 and field devices.
9. **Records.** The spray log, checked against the region's record-keeping rules;
   import the aircraft's job records or add records by hand.
10. **Today** (or the **Farmer** switch). One decision for today in plain words, the
    spray-weather strip, crop stage and alerts, built for a phone.
11. **System.** The GPU server connection, settings (theme, area unit, operator,
    aircraft ID), jobs, data sources and licences, and the audit log.

The **Dark / Light** switch is in the top bar of every page.

## The GPU server (H200)

1. Start the console on the laptop.
2. Run `scripts\console\Connect-GPU-Server.cmd` and enter `user@host` (it remembers
   the last one). Type the server password when SSH asks — it is never stored.
3. Keep the window open. The worker appears under **System → GPU server**;
   iterations set to *Automatic* go there. Closing the window disconnects; the
   worker on the server stops by itself two minutes later.

The worker runs in the NVIDIA PyTorch container when Docker has it (else a venv),
on the idlest GPU (none if all are busy — iterations are CPU work anyway). Weed
detection is offered only when a trained run is found (`FA_DETECTOR_WEIGHTS`, by
default the newest `~/futureagri/ml/runs/*` with `run.json`).

## Live run (closed loop)

A spray job can correct itself while it is flown. On the **Mission** page, a proposed or
approved spray job has **Fly closed loop** (virtual farm) or **Start live run** (live field).

* Before take-off the twin freezes its prediction: weed density per cell with its spread,
  the targets, the dose each cell should get, sorties, litres and time.
* After every sortie the camera on the sprayer reports what it saw per cell. Only a
  difference of more than 2 standard deviations over at least 5 cells and 5 % of the cells
  seen counts as a deviation. The rest of the job is re-planned with the planner's own rules:
  cells already at 90 % of the rate drop out, under-dosed cells come back (at most twice),
  weedy cells the camera found join, clean ones leave. Every correction is logged with its reason.
* **Spot valve** (optional): the boom opens only where the camera sees weeds above the
  threshold, or did not see at all, so a cell never gets more than the plan.
* **The aircraft** (live field): the on-board computer posts message batches (schema
  `fa.live/1`, see `packages/fa_core/fa_core/live_msgs.py`) to `/api/live-run/ingest` with a
  device token (**Live → Add a device**, kind *camera*). A batch sent twice is counted once.
  Nothing is sent to the aircraft: **Download next sortie (KML)** gives the remaining targets
  for its app. **Freeze plan (open loop)** stops all corrections. **Finish and update the twin**
  hands what the camera saw to the twin as a partial scan; unseen cells keep what the twin knew.

The **Live run** page shows the drone, the camera footprint, the cells seen, where the field
differs from the prediction, the remaining targets, planned vs flown, the sortie log and the
corrections. What the closed loop gains, measured on simulated farms, is in
[ADR 0018](adr/0018-live-closed-loop.md).

## Field devices

**Live → Add a device** creates a soil probe, weather station, camera or a GPS
tracker for the drone and shows its token once. Devices POST readings to
`/api/ingest`:

```json
{"token": "…", "readings": [{"kind": "soil_moisture", "ts": "2026-06-01T06:00:00Z", "value": 23.5}]}
```

Out-of-range or malformed readings are rejected individually and reported back.
`python -m fa_console.emulate --token …` simulates a probe or tracker.

## What to trust, and what not yet

* The weed detector was trained on one German maize field (WeedsGalore). Its
  recall halves from 2.5 to 5 mm per pixel and is near zero at 10 mm (H200 sweep):
  scan at 2.5–5 mm, and recalibrate on your own labelled images before relying on
  weed maps elsewhere.
* Prices and costs per region are illustrative until set from local market data;
  regional drone rules are marked *unverified* where nobody has checked them.
* The K3-A Pro has no control port: plans reach it as KML and settings; flights come
  back as job records, a tracker or a recorded flight.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FA_HOME` | `~/.futureagri/console` | database, farms, jobs |
| `FA_DATA_CACHE` | `~/.cache/futureagri/data` | downloaded weather, soil, imagery, forecasts |
| `FA_BIND` / `FA_PORT` | `127.0.0.1` / `8099` | listen address (anything else exposes a console with no login) |
| `FA_LOCAL_WORKERS` | `1` | jobs run at once on this computer |
| `FA_OFFLINE` | unset | `1`: never fetch; climatology instead of archived forecasts |
| `FA_MAX_ORTHO_MB` | `4096` | largest orthomosaic upload |

## For developers

`make console-build` rebuilds the UI (Node 20+), `make test` runs the Python tests,
`make test-browser` the Playwright browser tests (screenshots in `runs/console_e2e`).
API docs are served at `/api/docs`. Design notes: `docs/adr/0017-mission-console.md`.
