# Future Agri Mission Console — design

**Date:** 2026-11-01 · **Status:** approved in conversation (user: "no login right now … a fully
interactive and workable software for our project") · **Path:** architectural

## 1. Understanding

**What the user asked for.**
- A full, usable dashboard that takes a whole farm's data and makes its digital version.
- After a scan, the farm appears as a full **3D, marked virtual farm** that anyone can explore in detail.
- **Live data feeds and readings.**
- A fully accurate **3D digital twin of the K3-A Pro sprayer**.
- **Iterate with parameters**, while the simulation runs the live data and the models together.
- **Iterations and a final plan, including how long each step takes.**
- Production grade, deployable in the field, and used by farmers and by the team.
- Add anything they missed.

**Answer to the design question.**
- **No login for now.**
- The software must **actually run the project's mission**, i.e. the closed loop: camera-drone scan → digital farm → twin iterations → plan → K3-A Pro flight → records → twin update.

**Use the H200 whenever it helps.**

**Assumptions (stated to the user; uncontradicted).**
- It is a web app: phone, tablet or laptop browser, installable as a PWA, and works offline from cache.
- It runs on a laptop or field box and deploys unchanged to a server.
- Farmers get a simple multilingual **Today** view; the team gets everything.
- The K3-A Pro has no telemetry port, so live drone tracking needs an add-on tracker. Until one is fitted, the 3D drone plays simulated missions.
- The 3D farm is built from the elevation model, Sentinel-2 imagery and twin state. It is not photogrammetry.

**Success criteria.**
- From a fresh install, the operator can do the following without the CLI, and every number traces to the existing models and real data sources:
  1. create a farm (template site, or any location with an auto-detected or uploaded boundary);
  2. build its digital farm;
  3. explore it in 3D with marked weeds, disease and zones;
  4. tune and fly the drone twin in 3D;
  5. run the iterations;
  6. approve a plan with timings;
  7. run the mission day by day: scan, decide, fly, record, update;
  8. export the K3-A Pro files;
  9. see live weather, forecast, satellite and sensor data;
  10. give a farmer a translated Today screen.

## 2. Approach

Three options were weighed:
- **(Chosen) One web app on the existing Python stack.** A FastAPI service, `fa_console`, plus a React/TypeScript SPA. Every model runs directly behind it.
- **Rejected: desktop app.** It would be Windows-only and heavier.
- **Rejected: static dashboard.** It cannot run models or take live data.

## 3. Architecture

```
apps/console/                React 19 + TS + Vite SPA (three.js 3D, Leaflet 2D, Chart.js)
   └─ build → services/console/fa_console/static/
services/console/fa_console/
   app.py        FastAPI app: /api/* routers, SPA + PWA static, error handlers, /api/health
   config.py     env settings: FA_HOME (data dir), FA_DATA_CACHE, FA_BIND, FA_LOCAL_WORKERS
   store.py      SQLite (WAL) + artefact files under FA_HOME/farms/<id>/
   jobs.py       persistent job queue; local worker threads; remote-worker HTTP protocol; SSE
   farms.py      create / build (sim | field) / load farm objects (pickle cache)
   world.py      World adapters: VirtualWorld (ground truth) | RealWorld (live data + uploads)
   sessions.py   per-farm SeasonSession registry, action log, replay-on-restart
   scene.py      3D scene payload (terrain, imagery, cells, layers, markers)
   dronesvc.py   spec payload for 3D, mission simulation with track, presets
   live.py       weather (POWER recent + latest ECMWF ENS), spray outlook, Sentinel-2, alerts
   devices.py    sensor/tracker registry, token ingest, emulator
   records.py    spray log, JIYI job-record import, compliance fields
   exports.py    KML, settings sheet, survey plan, CSV, ICS, PDF, farm bundle
   advice.py     farmer Today card
   worker.py     remote worker CLI (`python -m fa_console.worker --server … --token …`)
   routes/       one router per area
services/runner/fa_runner/session.py   SeasonSession: the closed loop as a stepper
                                       (run_season becomes a thin wrapper — one code path)
```

### 3.1 Key changes to existing code (all behaviour-preserving)

- **`SeasonSession`** (`fa_runner/session.py`) extracts `run_season`'s day loop into a stepper.
  - **Interface:**
    - `step()` runs one morning; `run_until(day, stop_on_decision)` runs until a day or a decision.
    - Operations are `proposed → approved → flown | dropped | rejected`.
    - `approve / defer / reject / fly_now(settings)`; `scan_now`; `finish() -> SeasonResult`.
  - **Daily trace:**
    - each day: weather, truth summary (sim only) and twin state;
    - per-cell snapshots (LAI, cover, weeds, blight, cumulative dose) weekly and on event days.
  - `run_season(...)` is `SeasonSession(approvals="auto").run_until(harvest); finish()`.
  - **Proof of identity:** a test asserts identical `SeasonResult` to the pre-refactor values for fixed seeds.
- **`World` protocol:**
  - `weather(d)`, `last_observed()`, `advance_to(d)`, `scan(d, plan, rng, crop, das)`, `apply(as_applied)`, `truth_state()`, `harvest()`.
  - **`VirtualWorld`** wraps `GroundTruthFarm`.
  - **`RealWorld`** gives:
    - observed NASA POWER days, available with about a 3-day lag;
    - uploaded scans;
    - recorded flights.
    
    It has no truth and no scoring.
- **`simulate_mission(..., record_track=True)`** adds `MissionResult.track`: keyframes `{t, x, y, z, soc, v_cell, liquid_l, spraying, flow_lpm, phase, sortie}` at segment ends, for 3D playback. KPIs are unchanged (asserted by a test).
- **`power_recent()`** fetches NASA POWER through today and drops the trailing days not yet published, instead of raising.
- **`latest_issue(source)`** returns the newest ECMWF/GEFS init time in the archive. Verified on 2026-11-01: IFS ENS is current to 2026-10-31 00 UTC.
- **`s2_rgb_chip(polygon, start, end)`** returns a true-colour PNG of the clearest scene, using the Earth Search `visual` COG, for the 3D ground texture and the Live page.
- **`import_scan(geojson, grid, gsd)`** turns detection points (weed / disease, with confidence) and an optional per-cell cover table into a `ScanObservation`.
  - It applies the measured detector recall and FP model, then `build_digital_farm`.

### 3.2 Farm modes
| | **Virtual test farm** (`sim`) | **Live field** (`field`) |
|---|---|---|
| Season | a past season with complete data | the current season (sowing date to expected harvest) |
| Truth | hidden `GroundTruthFarm` from real data | none |
| Clock | virtual; the operator steps it | real; the twin is current to the last observed weather day |
| Scan | simulated camera-drone scan | upload detections GeoJSON (or a simulated preview) |
| Flight | drone twin flies, deposits on the truth | export KML + settings → fly → import JIYI record → as-applied |
| Iterations | perfect run on this season | perfect run rehearsed on the **same field's previous season** |
| Scoring | yield, net, compared with the truth | none (twin forecasts only) |

### 3.3 Data model (SQLite)

**Tables:**
- `farms`: id, name, mode, template, crop, region, country, year, lat, lon, area_ha, cell_m, polygon, sowing, harvest, status, meta.
- `jobs`: id, farm_id, kind, status, progress, message, params, result, error, worker, timestamps.
- `events`: audit log and timeline.
- `plans`: draft, approved or superseded; payload.
- `operations`: mission operations mirrored from the session.
- `records`: spray log.
- `devices`: kind and token hash.
- `readings`.
- `settings`.

**Artefacts:** under `FA_HOME/farms/<id>/`:
- `farm.pkl` (VirtualFarm or FieldFarm);
- `scene.json`;
- `imagery.png`;
- job results (`*.json`);
- exports.

### 3.4 Jobs and compute

- **Job kinds:** `build`, `perfect`, `season` (auto replicate), `mission`, `session_run`, `detect`.
- **Local workers:** N threads (default 1; the perfect run uses its own process pool).
- **Remote workers:**
  - `POST /api/workers/claim` takes a token and capabilities.
  - Then `/progress` and `/complete`.
  - Artefacts download from `/api/workers/artefact/<job>`.
- **Routing:** `target=auto` prefers a remote worker seen in the last 60 s.
- **H200:** `Connect-GPU-Server.cmd` runs one SSH session (the user types the password; Claude never does). It:
  - streams the code bundle;
  - opens a reverse tunnel `-R 18099:127.0.0.1:8099`;
  - starts `fa_console.worker` on the server.
  
  There, perfect runs use its many cores, and `detect` runs RF-DETR on uploaded orthomosaic tiles where the weights exist.
- **Progress** goes over SSE (`/api/jobs/<id>/events`).

## 4. Screens

**Global:** Farms · Today (farmer) · System.

**Farm-scoped:**
1. **Mission** (home of a farm): the loop as a stepper (Scan → Digital twin → Iterate → Plan → Fly → Record → Update).
   - A day/clock bar: sim step / to next decision / to harvest; field: refresh live.
   - The operations queue with approve / defer / reject / fly / export / record.
   - Go/no-go for today: wind, gust, temperature, rain, region rules.
   - An event timeline and the twin's yield forecast.
2. **3D Twin.**
   - **Scene:** terrain (DEM, exaggeration slider), S2 imagery or procedural ground, per-cell crop canopy (height and colour from LAI/cover, disease tint).
   - **Layers:** canopy, cover, zones, weeds, blight, dose, wetness, yield.
   - **Markers:** weed hotspots, blight foci, sensors, home point, buffer zone, routes.
   - **Interaction:** a time slider over the session trace; a cell inspector; a legend.
3. **Drone Twin.**
   - **3D airframe to the spec sheet:** 1300 mm wheelbase, 4 × 0.881 m rotors, 10 L tank, 4 nozzles, 920×920×480 mm unfolded, skids, GNSS mast.
   - **Spec panel:** inferred values flagged.
   - **Parameter panel:** height, speed, spacing, rate, RTK, pump, route angle, battery sets, chargers, weather.
   - Simulate on the farm, then 3D playback over the 3D farm with spinning rotors, spray plumes when spraying, and a track trail.
   - **Gauges:** battery % and V/cell, tank L, flow, speed, height, sortie, elapsed time.
   - Event log, KPI cards, and A/B compare of two runs.
4. **Simulate:** season charts linked to the 3D slider.
   - truth vs twin LAI;
   - blight and weeds;
   - weather and forecasts used;
   - decisions and flights.
5. **Iterate:** perfect-run job (seeds, members, compute target) with a live log.
   - Ranked plans (E[net], CVaR20, score) and the flight ranking.
   - Logistics (battery sets × chargers), value at risk, warnings.
   - "Use this plan" turns a result into a draft plan.
6. **Plan:** approved plan and versions.
   - **Operations schedule:** expected dates or windows per operation, with time, sorties, swaps, litres and crew time.
   - Season totals; a battery and charger recommendation.
   - **Exports:** KML, settings, survey plan, CSV, ICS, PDF.
7. **Live:**
   - **Weather:** observed (POWER, last 30 days) and the latest ECMWF ensemble (bands).
   - **Spray outlook:** per-day probability that a member is flyable under the aircraft and region rules.
   - **Satellite:** S2 cover series and the latest true-colour image.
   - **Devices:** sensors and tracker readings, and the live drone position on the 3D farm.
   - Alerts.
8. **Records:** the spray log (simulated, imported, manual).
   - JIYI import with a per-row error report.
   - Region compliance fields; CSV and PDF export.
9. **Today (farmer):**
   - **Content:** one large decision card ("Spray today / Wait / No action"), the reason, the next operation and window, field health, alerts.
   - **Form:** en, hi, es, pt; large touch targets; offline cache.
10. **System:**
    - jobs (cancel) and workers (local or GPU server, with connect instructions and token);
    - data sources and licences;
    - settings;
    - audit log;
    - version.

## 5. API (prefix `/api`)

- **System:**
  - `GET health`, `GET meta` (templates, regions, countries, version).
  - `GET/POST settings`.
- **Farms:**
  - `GET/POST farms`; `GET/DELETE farms/{id}`.
  - `POST farms/{id}/build` → job.
  - `GET farms/{id}/scene`, `GET farms/{id}/imagery.png`.
  - `POST farms/detect-boundary` (lat, lon, country → candidate polygons).
- **Session (mission):**
  - `GET farms/{id}/session` (state, ops, trace summary).
  - `POST farms/{id}/session/{step|run|approve|defer|reject|fly|scan|reset|finish}`.
  - `GET farms/{id}/session/trace`, `GET farms/{id}/session/snapshots`.
- **Drone and plans:**
  - `GET drone/spec`; `POST farms/{id}/mission` (settings, weather) → result with track.
  - `POST farms/{id}/perfect` → job.
  - `GET farms/{id}/plans`, `POST farms/{id}/plans` (from a job result), `POST plans/{id}/approve`.
- **Live:**
  - `GET farms/{id}/live/{weather|outlook|satellite|alerts}`.
- **Devices:**
  - `GET/POST farms/{id}/devices`.
  - `POST ingest` (device token, batch readings).
  - `GET farms/{id}/readings`.
- **Records:**
  - `GET/POST farms/{id}/records`; `POST farms/{id}/records/import`.
- **Exports:**
  - `GET farms/{id}/export/{kml|settings|survey|plan.csv|plan.ics|plan.pdf|records.csv|bundle}`.
- **Today:**
  - `GET farms/{id}/today?lang=`.
- **Jobs and workers:**
  - `GET jobs`, `GET jobs/{id}`, `GET jobs/{id}/events` (SSE), `POST jobs/{id}/cancel`.
  - `POST workers/{claim|progress|complete}`, `GET workers`.

## 6. Error handling and safety

- **Network and access:**
  - With no login, the server binds `127.0.0.1` by default.
  - `FA_BIND=0.0.0.0` shows a "no login: anyone on this network can operate" banner.
  - Device ingest and remote workers require per-device and worker tokens, stored as hashes.
- **Uploads:**
  - Capped at 50 MB, with the type checked.
  - Never written outside `FA_HOME`.
  - A string is never treated as a path (the existing `jiyi_io` rule).
- **Data-source failures** surface as `DataUnavailable(layer)`. The UI shows which layer failed and what still works; cached data is used offline.
- **Jobs:**
  - They survive restarts: running jobs are marked failed with "server restarted", and the operator can re-run them.
  - Sessions replay their action log.
- **Operational safety:**
  - Every plan carries the region pack's `verified` flag and illustrative-price warning.
  - Flights failing the go/no-go cannot be marked flown in field mode without an explicit override, which is logged.

## 7. Testing

- **pytest:**
  - store, jobs and the worker protocol;
  - farm create/build from cached archetype data;
  - scene payload;
  - mission track (monotonic time, ends landed, KPIs unchanged);
  - `SeasonSession`: auto mode equals `run_season`; manual approve/defer/reject; replay restores state;
  - `RealWorld` with fixture weather;
  - live endpoints with mocked sources;
  - device ingest auth; JIYI import; exports (KML parses, ICS valid, PDF bytes);
  - Today in 4 languages.
- **Network tests** stay behind `FA_NETWORK=1`.
- **e2e (Python Playwright against uvicorn + built SPA):**
  1. create a sim farm from cache;
  2. 3D twin renders with no console errors;
  3. drone playback runs;
  4. mission step / approve / fly;
  5. iterate results view;
  6. plan exports;
  7. farmer language switch.
  
  Screenshots are kept in `runs/console_e2e/`.

## 8. Delivery

- `python -m fa_console` serves on `127.0.0.1:8099`.
- `Start-FutureAgri.cmd` (Windows) creates a venv, installs requirements, starts the server and opens the browser.
- Dockerfile and a compose service.
- The data cache for the template sites ships, so they build offline.
- Docs: `docs/console.md` (operator guide, device/tracker ingest format, GPU worker) and ADR 0017.

## 9. Out of scope (now)

- User accounts and roles: deferred by the user. The store keeps an `actor` field so they can be added.
- Flight control of the K3-A Pro: it has no SDK; export and import only.
- Photogrammetry and orthomosaic stitching: bring detections or an orthomosaic.
- Native mobile apps: the PWA covers phones.
- Non-potato disease models: stated as not modelled, as in ADR 0015.
