# Future Agri Mission Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A field-deployable web console that runs the Future Agri mission loop end to end:
- a 3D digital farm;
- a 3D K3-A Pro twin;
- an interactive season session;
- iterations and plans with timings;
- live feeds, records, a farmer Today view;
- a GPU-server worker.

**Architecture:**
- **Back end:** a FastAPI service, `fa_console`, over a SQLite store and a persistent job queue. It reuses `fa_farm`, `fa_scan`, `fa_drone` and `fa_runner` directly.
- **Season loop:** `run_season` is refactored into a `SeasonSession` stepper over a `World` (virtual truth or real data).
- **Front end:** a React/TypeScript SPA (three.js, Leaflet, Chart.js), built into the service's static folder.

**Tech Stack:**
- Python 3.13 (code stays 3.11-compatible), FastAPI, uvicorn, SQLite (stdlib), reportlab.
- React 19.3.1, TypeScript, Vite, three 0.187.0, leaflet 1.9.4, chart.js 4.6.1.
- Python Playwright for e2e.

**Spec:** `docs/superpowers/specs/2026-11-01-mission-console-design.md`

## Global Constraints

- **No login.** Bind `127.0.0.1` by default. `FA_BIND` overrides it, and when it is not loopback a "no login" banner shows. Every write stores `actor` (default `"operator"`).
- **No new Python runtime dependencies:** fastapi, uvicorn, pydantic, python-multipart and reportlab are already installed. SSE uses `StreamingResponse`.
- **npm:** dependencies are pinned to exact versions. Installs use `NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 npm_config_noproxy=localhost,127.0.0.1 npm_config_cafile=/root/.ccr/ca-bundle.crt`.
- **Uploads:** ≤ 50 MB. Files are never written outside `FA_HOME`, and a string is never treated as a path.
- **Provenance:**
  - Every data layer keeps its `Provenance`, and `/api/sources` lists them.
  - Plans show the region pack's `verified` flag and the illustrative-price warning.
- **Global product:** no India defaults. Area shows in ha, with acre and mu as display options.
- **Tests:**
  - The existing 224 tests stay green.
  - Network tests are marked `network` (`FA_NETWORK=1`); e2e tests are marked `e2e` (`FA_E2E=1`).
- **Limits:**
  - Cells per farm ≤ 20 000 (cell size is raised automatically).
  - Field area ≤ 500 ha.

## Review Focus

1. **A location where a data layer is missing** (no FTW field nearby, no Sentinel-2 scenes, NASA POWER lag):
   - the build job fails naming the layer, or degrades with a warning;
   - an uploaded boundary still builds.
   
   Tests are in Task 5.
2. **Offline or failed live fetch:** the endpoints return the last cached payload with `stale: true`, or `available: false` with a reason, and never a 500. Tests are in Task 9.
3. **Server restart mid-job or mid-session:**
   - running jobs become `failed` ("server restarted");
   - sessions replay their action log to the same day and operations.
   
   Tests are in Tasks 4 and 7.
4. **Malformed or oversized uploads** (bad JIYI rows, detections outside the field, wrong geometry, > 50 MB):
   - a per-row report, with outside points counted;
   - 413 or 422, never a 500.
   
   Tests are in Tasks 3 and 10.
5. **Double actions:** a second "run to harvest" or a step while a session job runs gets 409 `session busy`, with state unchanged. Tests are in Task 7.

---

### Task 1: Drone flight track for 3D playback

**Files:**
- Modify: `services/drone/fa_drone/mission.py`
- Test: `tests/test_drone_track.py`

**Interfaces:**
- Produces:
  - `MissionResult.track: list[dict]`;
  - `simulate_mission(..., record_track: bool = True)`.
- Keyframe keys: `t, x, y, z, soc, v_cell, liquid_l, spraying, flow_lpm, phase, sortie`, where `phase ∈ {takeoff, ferry, spray, turn, rth, land, ground, relocate, landed}`.

- [ ] **Step 1: Tests**

```python
def test_track_is_time_ordered_and_ends_landed(): # t non-decreasing; track[-1]["phase"]=="landed"; track[-1]["z"]==0
def test_spray_keyframes_inside_field_bounds():    # spraying frames within grid.bounds_local ± 10 m
def test_kpis_identical_with_and_without_track():  # kpis equal; dose arrays allclose
def test_sortie_increments_after_rth():            # max(sortie) == kpis["sorties"]
def test_liquid_never_increases_within_a_sortie():
```

- [ ] **Step 2:** Run `pytest tests/test_drone_track.py -q`. Expected: FAIL (no `track`).
- [ ] **Step 3:** Record a keyframe at the end of every `ferry`, `vertical`, spray chunk, turn and `ground_ops`, and at take-off and landing.
  - Spray frames include the last GNSS error sample.
  - `flow_lpm = l_per_m * v * 60` while spraying, else 0.
- [ ] **Step 4:** Run the tests and `pytest tests/test_drone_mission.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(drone): flight track keyframes for 3D playback`.

### Task 2: SeasonSession — the closed loop as a stepper

**Files:**
- Create: `services/runner/fa_runner/world.py`, `services/runner/fa_runner/session.py`
- Modify: `services/runner/fa_runner/season.py` (`run_season` becomes a wrapper)
- Test: `tests/test_session.py`, `tests/fixtures/session_golden.json`

**Interfaces:**
- **World protocol:**
  - Properties: `grid`, `sowing`, `harvest`, `blight_modelled`.
  - Methods:
    - `weather(d) -> dict | None`;
    - `last_observed() -> date`;
    - `advance_to(d)`;
    - `scan(d, gsd_m, camera, rng, crop) -> ScanObservation | None`;
    - `apply(aa: AsApplied)`;
    - `truth_state() -> CellState | None`;
    - `harvest_result() -> HarvestResult | None`.
  - `VirtualWorld(vf, seed)` implements it.
- **`Operation`** (dataclass) has fields `id, product, due, status, targets, created, reason, settings, result`.
  - Statuses: `proposed | approved | flown | dropped | rejected`.
  - Products: `herbicide | fungicide | scan`.
  - The id is deterministic: `f"{product}-{due.isoformat()}-{n}"`.
- **`SeasonSession`:**
  - `SeasonSession(vf, policy="twin", seed=0, feed=None, spec=None, flight=None, scan_das=45, scan_gsd_m=0.005, camera="p4rtk", scout_gsd_m=0.01, plan_members=10, objective="mean", cache=None, expected_yield_kg_ha=None, approvals="auto", world=None, trace=True, snapshot_every=7)`.
  - Attributes: `day, done, ops: dict[str, Operation], events: list[dict], trace: list[dict], snapshots: dict[str, dict[str, np.ndarray]], actions: list[dict]`.
  - Methods:
    - `step() -> list[dict]`;
    - `run_until(day=None, stop_on_decision=False, max_days=None) -> list[dict]`;
    - `approve(op_id)`, `reject(op_id, reason="")`, `defer(op_id, days)`;
    - `fly_now(op_id, settings=None) -> dict`;
    - `scan_now(gsd_m=None) -> dict`;
    - `finish() -> SeasonResult`;
    - `state() -> dict` (JSON-able);
    - classmethod `restore(kwargs, actions) -> SeasonSession`.
- **Events:** `{"day","kind","text",...}` with kinds `scan, plan, proposal, approved, rejected, deferred, flight, no_fly, dropped, harvest`.
- **Trace row keys:** `day, das, tmax, tmin, rain_mm, wind_ms, truth_lai, truth_cc, truth_dvs, truth_weeds_m2, truth_blight, twin_lai, twin_dvs, twin_twso`.
- **Snapshot arrays:** float16 `lai, cc, weeds, blight, dose_fungicide, dose_herbicide`.

- [ ] **Step 1:** BEFORE any refactor, write `tests/fixtures/session_golden.json`:
  - contents: `run_season(...).summary()` plus the `missions_log` days;
  - inputs: `_vf(inoculum=True)` and `_vf(inoculum=False)` from `test_runner`, policies `twin` and `calendar`, seed 0, `plan_members=6`, climatology feed.
- [ ] **Step 2: Tests**

```python
def test_auto_session_matches_golden_run_season():     # summary == golden for all 4 cases
def test_manual_mode_stops_on_first_proposal():        # run_until(stop_on_decision=True) -> an op with status "proposed"; day < harvest
def test_approving_everything_equals_auto():           # same SeasonResult summary as auto
def test_rejected_herbicide_is_never_flown():          # no herbicide in missions_log
def test_defer_moves_due_date():                       # op.due == old + days
def test_trace_has_one_row_per_day_and_snapshots():    # len(trace) == (harvest - sowing).days; snapshot keys
def test_restore_replays_actions_to_same_state():      # restore(kwargs, s.actions).state() == s.state()
```

- [ ] **Step 3:** Run `pytest tests/test_session.py -q`. Expected: FAIL (module missing).
- [ ] **Step 4:** Implement it:
  - Move the day loop into `step()`. Keep the RNG call order exactly as it is: herbicide, then fungicide, then flights.
  - With `approvals="manual"`, new jobs become `proposed`; only `approved` ones fly.
  - Actions are logged as `{"at": day_iso, "action", "op", "params"}`.
- [ ] **Step 5:** Run `pytest tests/test_session.py tests/test_runner.py -q`. Expected: PASS.
- [ ] **Step 6:** Commit `feat(runner): SeasonSession stepper; run_season wraps it`.

### Task 3: Live sources and scan import

**Files:**
- Modify: `services/farm/fa_farm/sources/weather.py`, `forecasts.py`, `satellite.py`
- Create: `services/scan/fa_scan/importer.py`
- Test: `tests/test_live_sources.py`

**Interfaces:**
- `power_recent(lat, lon, start, cache, today=None) -> (records, prov, elev)`. It drops trailing days that are not yet published, and raises `DataUnavailable("weather")` if none remain.
- `latest_issue(source: ForecastSource) -> date`.
- `latest_forecast(lat, lon, cache, history, n_members=20) -> (members, source, issue, prov)`.
- `s2_rgb_chip(polygon_wgs84, start, end, cache, margin_m=60.0, max_cloud=40.0) -> (png: bytes, meta: dict, prov)`, where meta has `date, cloud, bounds_lonlat, scene`.
- `import_scan(geojson, grid, gsd_m, day_index, cover_cells=None, dvs=None, detector=None) -> (ScanObservation, report)`, where report has `points, weeds, disease, outside, bad, errors`.

- [ ] **Step 1: Tests**

```python
def test_power_recent_drops_trailing_missing_days(monkeypatch):  # fixture with last 3 days -999 -> 3 fewer records, no raise
def test_latest_issue_reads_max_init_time(monkeypatch):           # fake dataset init_time -> date
def test_import_scan_counts_outside_and_bad_features():          # 1 outside polygon, 1 LineString -> outside==1, bad==1
def test_import_scan_densities_are_recall_corrected():           # density > raw count / cell area when recall < 1
@pytest.mark.network
def test_s2_rgb_chip_flevoland(): # png starts b"\x89PNG"; meta["date"] within range
```

- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement them. `s2_rgb_chip` reads the Earth Search `visual` asset window (the lowest-cloud scene) and writes a PNG with stdlib `zlib`.
- [ ] **Step 4:** Run them. Expected: PASS (the network test passes with `FA_NETWORK=1`).
- [ ] **Step 5:** Commit `feat(sources): live weather/forecast, S2 true colour, scan import`.

### Task 4: Console core — config, store, jobs, app

**Files:**
- Create in `services/console/fa_console/`: `__init__.py`, `__main__.py`, `config.py`, `store.py`, `jobs.py`, `app.py`, `routes/__init__.py`, `routes/system.py`, `routes/jobs.py`, `routes/workers.py`
- Modify: `tests/conftest.py` (add `services/console` to the path)
- Test: `tests/test_console_core.py`

**Interfaces:**
- **`config.Settings`** (dataclass) reads these env variables:

  | Field | Env variable | Default |
  |---|---|---|
  | `home` | `FA_HOME` | `~/.futureagri/console` |
  | `cache` | `FA_DATA_CACHE` | |
  | `bind` | `FA_BIND` | `127.0.0.1` |
  | `port` | `FA_PORT` | `8099` |
  | `local_workers` | `FA_LOCAL_WORKERS` | `1` |
  | `max_upload_mb` | | `50` |
  | `offline` | `FA_OFFLINE` | |
  | `worker_token` | | generated and persisted |

- **`store.Store(path)`:**
  - farms CRUD;
  - `add_event / events`;
  - `create_job / update_job / get_job / list_jobs / claim_job(kinds, worker)` (atomic);
  - plans, operations, records, devices, readings, settings;
  - `farm_dir(fid) -> Path`.
- **`jobs.JobManager(store, settings)`:**
  - `register(kind, fn)`, with `fn(job, ctx) -> dict`;
  - `submit(kind, farm_id, params, target="auto")`;
  - `start()`, `stop()`, `cancel(id)`, `events(id)` (async generator).
  - `ctx.progress(frac, msg)`, `ctx.check()` (raises `JobCancelled`).
  - Remote: `claim(token, kinds, worker)`, `report(token, id, frac, msg)`, `complete(token, id, result=None, error=None)`.
- **`app.create_app(settings=None) -> FastAPI`:**
  - `/api/health`, `/api/meta`, `/api/settings`, `/api/sources`;
  - `/api/jobs[...]`, `/api/workers[...]`;
  - serves `static/` with SPA fallback.

- [ ] **Step 1: Tests**

```python
def test_store_farm_crud_and_events(tmp_path):
def test_job_runs_and_reports_progress(tmp_path):        # status queued->running->done; progress 1.0; result saved
def test_job_failure_and_cancel(tmp_path):               # exception -> failed with error text; cancel -> cancelled
def test_restart_marks_running_jobs_failed(tmp_path):    # new JobManager -> "server restarted"
def test_remote_worker_claim_requires_token(tmp_path):   # bad token 401; good token gets job; complete -> done
def test_health_meta_and_spa_fallback(tmp_path):          # /api/health ok; /api/meta templates A1..A6; /farms/x serves index.html
def test_sse_streams_job_events(tmp_path):
```

- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement it:
  - SQLite with WAL, `check_same_thread=False`, a lock around writes;
  - JSON columns;
  - job results in `farm_dir/jobs/<id>.json`.
- [ ] **Step 4:** Run them. Expected: PASS.
- [ ] **Step 5:** Commit `feat(console): store, job queue, remote worker protocol, app skeleton`.

### Task 5: Farms — create, build (sim | field), scene

**Files:**
- Create: `services/console/fa_console/farms.py`, `scene.py`, `routes/farms.py`
- Modify: `services/farm/fa_farm/build.py` (add `FieldFarm`, `build_field_farm`)
- Test: `tests/test_console_farms.py`

**Interfaces:**
- **`FarmCreate`** (pydantic) fields:

  | Field | Type | Default |
  |---|---|---|
  | `name` | | |
  | `mode` | `"sim"` \| `"field"` | |
  | `template` | `A1..A6` | |
  | `site_index` | `int \| None` | |
  | `lat`, `lon` | `float \| None` | |
  | `country` | `str \| None` | |
  | `polygon` | GeoJSON \| None | |
  | `year` | `int \| None` | |
  | `sowing` | `date \| None` | |
  | `region` | `str \| None` | |
  | `cell_m` | float in 2..50 | 10 |
  | `seed` | int | 0 |

- **`FarmService(store, settings, jobs)`:**
  - `create(req) -> dict`;
  - `build(job, ctx) -> dict` (registered as `build`);
  - `load(fid)` (pickle + LRU);
  - `delete(fid)`;
  - `detect_boundary(lat, lon, country, min_ha, max_ha) -> list[dict]`.
- **`build_field_farm(template_id, polygon_wgs84, sowing, cache, cell_m=10.0, today=None, region=None) -> FieldFarm`.** `FieldFarm` fields: `spec, observed, history, s2_series, harvest, provenance, warnings`.
- **`scene.build_scene(farm_obj, cache) -> dict`** keys:
  - `origin, polygon, bounds, terrain{nx,ny,x0,y0,dx,dy,heights_b64,min,max}, cells{n,cell_m,x_b64,y_b64}, layers{zone,twi}, imagery, home, warnings`.
  - Terrain falls back to flat when the DEM is missing.
- **`scene.imagery_png(farm_obj, cache) -> bytes | None`.**

- [ ] **Step 1: Tests** (the builder is monkeypatched to `test_runner._vf`)

```python
def test_create_sim_farm_and_build_job(tmp_path):        # job done; farm status "ready"; farm.pkl exists
def test_build_failure_names_layer(tmp_path):            # DataUnavailable("field boundaries") -> job failed, message contains "field boundaries"
def test_uploaded_polygon_skips_boundary_detection(tmp_path):
def test_area_limit_and_cell_cap(tmp_path):              # 600 ha -> 422; 300 ha @5 m -> cell_m raised, cells <= 20000, warning
def test_scene_payload_shapes(tmp_path):                 # len(x)==n cells; terrain flat fallback + warning
def test_delete_farm_removes_files(tmp_path):
@pytest.mark.network
def test_build_real_template_farm_A6(tmp_path):
```

- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement it. Arrays are encoded as base64 little-endian float32 (positions) or float16 (layers).
- [ ] **Step 4:** Run them. Expected: PASS.
- [ ] **Step 5:** Commit `feat(console): farms (sim/field), build jobs, 3D scene payload`.

### Task 6: Drone twin service

**Files:**
- Create: `services/console/fa_console/dronesvc.py`, `routes/drone.py`
- Test: `tests/test_console_drone.py`

**Interfaces:**
- **`drone_spec_payload() -> dict`** keys:
  - `name, wheelbase_mm, unfolded_mm, folded_mm, rotor_diameter_m, n_rotors, tank_l, nozzles[{x,y}]`;
  - `limits{speed, work_speed, tilt, vz, wind, gust, link_range}`;
  - `battery{..., inferred}`;
  - `pump_lpm`, `swath_m`, `hover_points`, `notes`.
- **`simulate(farm_obj, settings: MissionSettings, weather: Weather, targets="all"|"weeds", seed=0) -> dict`** keys:
  - `feasible, reasons, warnings, kpis, track, route[{start,end}], events, dose_b64, home, settings`.
- **Routes:**
  - `GET /api/drone/spec`;
  - `POST /api/farms/{fid}/mission` (synchronous; fields ≤ 60 ha; otherwise a `mission` job).

- [ ] **Step 1: Tests**

```python
def test_spec_payload_matches_sheet():           # wheelbase 1300, rotor 0.8814, tank 10, 4 nozzles
def test_mission_endpoint_returns_track(tmp_path):  # feasible; track[-1]["phase"]=="landed"; kpis keys
def test_mission_rejects_bad_settings(tmp_path):     # speed 0 -> 422
def test_mission_wind_too_high_is_infeasible(tmp_path):  # wind 9 -> feasible False with reason
```

- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Run them. Expected: PASS.
- [ ] **Step 5:** Commit `feat(console): drone twin spec + mission simulation API`.

### Task 7: Mission sessions (sim + field)

**Files:**
- Create: `services/console/fa_console/sessions.py`, `routes/session.py`
- Modify: `services/runner/fa_runner/world.py` (add `RealWorld`)
- Test: `tests/test_console_session.py`

**Interfaces:**
- **`RealWorld(field_farm, scans: dict[date, ScanObservation] | None = None)`.**
  - `weather(d)` returns observed records only; `truth_state()` and `harvest_result()` return `None`.
  - `scan()` returns the uploaded scan for d or later, else `None`, and the session keeps the scan op `proposed`.
- **`SessionService(store, settings, farms, jobs)`:**
  - `get(fid)`, `act(fid, action, params) -> dict` (409 when busy);
  - `state(fid)`, `trace(fid)`, `snapshots(fid, day=None)`;
  - `reset(fid, plan=None)`;
  - `upload_scan(fid, geojson, gsd_m) -> report`;
  - `record_flight(fid, op_id, record) -> dict`.
  - Actions are stored as events of kind `session_action` and replayed on first `get` after a restart.
- **Plan config** comes from the approved plan: `policy, flight, scan_das, scan_gsd_m`. The default is `twin` with the default flight.
- **Routes:**
  - `GET /api/farms/{fid}/session`;
  - `POST /api/farms/{fid}/session/{step|run|approve|defer|reject|fly|scan|reset|finish|upload-scan|record}`;
  - `GET .../session/trace`, `GET .../session/snapshots`.
  - `run` to harvest goes through a `session_run` job.

- [ ] **Step 1: Tests**

```python
def test_step_advances_one_day(tmp_path):
def test_run_stops_on_decision_and_approve_flies(tmp_path):
def test_restart_replays_to_same_state(tmp_path):        # new SessionService -> same day, same ops
def test_concurrent_action_gets_409(tmp_path):           # lock held -> 409 "session busy"
def test_field_session_waits_for_weather_and_scan(tmp_path):  # RealWorld with weather to D -> cannot step past D+1; scan op proposed until upload
def test_finish_scores_only_in_sim(tmp_path):
```

- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Run them. Expected: PASS.
- [ ] **Step 5:** Commit `feat(console): interactive mission sessions with replay`.

### Task 8: Iterations and plans

**Files:**
- Create: `services/console/fa_console/plans.py`, `routes/plans.py`
- Test: `tests/test_console_plans.py`, `tests/fixtures/perfect_small.json` (a recorded small perfect-run result)

**Interfaces:**
- **`perfect_job(job, ctx)`** (kind `perfect`) takes params `seeds=3, members=6, workers=None, quick=False`.
  - `quick` uses `flight_grid={heights:(2.0,3.0), speeds:(5.0,7.0), swaths:(4.0,5.0), rates:(15.0,), rtk:(False,True)}`.
  - For field farms it rehearses on `build_virtual_farm(template, sowing.year - 1, polygon_wgs84=…)`.
  - The result adds `seasons_summary[plan] = [summary + missions_log]`.
- **`make_plan(farm_meta, result, plan_name) -> dict`** keys:
  - `plan, policy, flight, scan, operations[{product, count_mean, first, median, last, time_min, sorties, swaps, litres, crew_min}]`;
  - `totals{drone_h, crew_h, litres, missions}`;
  - `logistics_pick`, `net_mean`, `cvar20`, `currency`, `warnings`.
  - `crew_min = time_min + 20` per mission (setup assumption, labelled).
  - `logistics_pick` is the cheapest packs × chargers within 90 % of the best ha/h.
- **Routes:**
  - `POST /api/farms/{fid}/perfect`;
  - `GET/POST /api/farms/{fid}/plans`;
  - `POST /api/plans/{pid}/approve`. Approving supersedes the old plan and resets the session with the new plan, with confirmation required when the session has progressed.

- [ ] **Step 1: Tests**

```python
def test_make_plan_operations_and_totals():         # from fixture: operations per product, totals consistent
def test_logistics_pick_rule():
def test_approve_supersedes_and_configures_session(tmp_path):
@pytest.mark.slow
def test_perfect_job_quick_runs(tmp_path):          # synthetic 1 ha farm, seeds=1, members=4
```

- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Run them. Expected: PASS.
- [ ] **Step 5:** Commit `feat(console): iterations job and plans with timings`.

### Task 9: Live feeds and devices

**Files:**
- Create: `services/console/fa_console/live.py`, `devices.py`, `emulate.py`, `routes/live.py`, `routes/devices.py`
- Test: `tests/test_console_live.py`

**Interfaces:**
- **`LiveService(store, settings, farms)`** caches to `FA_HOME/live/<fid>/<kind>.json` with a TTL (weather 3 h, satellite 12 h):
  - `weather(fid) -> {available, stale, observed[], forecast{issue, source, days[{day, tmax[p10,p50,p90], tmin[...], rain[...], wind[...]}]}}`;
  - `outlook(fid) -> {days[{day, p_flyable, limits}]}`, using `spray_window(region, spec)` over the members with `DAWN_WIND_FACTOR`;
  - `satellite(fid) -> {series[{date, cover}], latest{date, cloud, url}}`;
  - `alerts(fid) -> [{level, code, params}]`.
- **`DeviceService`:**
  - `create(fid, kind, name) -> {device, token}` (the token is shown once and stored as a sha256 hash);
  - `ingest(token, readings) -> {accepted, rejected[]}`;
  - `readings(fid, kind=None, since=None)`.
  - Device kinds: `soil_moisture, weather_station, tracker, camera`.
- **`emulate.py`** CLI: `python -m fa_console.emulate --server URL --token T --kind soil_moisture|tracker`.

- [ ] **Step 1: Tests**

```python
def test_weather_served_stale_when_offline(tmp_path, monkeypatch):   # fetch raises -> cached payload stale True
def test_weather_unavailable_without_cache(tmp_path, monkeypatch):   # available False + reason, HTTP 200
def test_outlook_probability_bounds(tmp_path, monkeypatch):          # 0<=p<=1, len(days)>=7 from fake members
def test_device_ingest_auth_and_validation(tmp_path):                 # bad token 401; bad reading rejected with reason
def test_tracker_readings_appear_as_positions(tmp_path):
```

- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Run them. Expected: PASS.
- [ ] **Step 5:** Commit `feat(console): live weather/forecast/outlook/satellite, devices and ingest`.

### Task 10: Records, exports, farmer Today

**Files:**
- Create: `services/console/fa_console/records.py`, `exports.py`, `advice.py`, `routes/records.py`, `routes/exports.py`
- Test: `tests/test_console_records.py`

**Interfaces:**
- **Records:**
  - `import_jiyi(fid, data: bytes, filename, mapping=None) -> {added, report}` (reuses `import_job_records`);
  - `add_manual(fid, rec)`;
  - `compliance(fid, rec) -> missing_fields` (from `region.rules.records`).
- **Exports:**
  - `kml(fid, op_id=None) -> bytes`;
  - `settings(fid) -> dict`;
  - `survey_kml(fid, gsd_m=0.01) -> bytes`;
  - `plan_csv`, `plan_ics`, `plan_pdf`;
  - `records_csv`;
  - `bundle_zip(fid) -> bytes`.
- **`advice.today(fid) -> {status: spray|wait|scan|none, code, params, next, window, health, alerts}`.** It is language-neutral; the SPA translates `code`s.

- [ ] **Step 1: Tests**

```python
def test_jiyi_import_reports_bad_rows(tmp_path):     # 1 bad row -> report lists row 3; good rows added
def test_upload_too_large_413(tmp_path):
def test_kml_parses_and_has_polygon(tmp_path):
def test_ics_has_event_per_operation(tmp_path):      # count("BEGIN:VEVENT") == len(ops)
def test_pdf_bytes(tmp_path):                        # startswith(b"%PDF")
def test_today_codes_cover_states(tmp_path):         # spray/wait/scan/none reachable
```

- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Run them. Expected: PASS.
- [ ] **Step 5:** Commit `feat(console): records, exports (KML/ICS/PDF/CSV/bundle), farmer Today`.

### Task 11: SPA scaffold, design system, Farms and New-farm wizard

**Files:**
- Create in `apps/console/`: `package.json` (exact pins), `vite.config.ts`, `tsconfig.json`, `index.html`
- Create in `apps/console/src/`: `main.tsx`, `api.ts`, `router.ts`, `i18n/{en,hi,es,pt}.json`, `i18n.ts`, `theme.css`, `components/*`, `pages/Farms.tsx`, `pages/NewFarm.tsx`
- Create: `tests/e2e/conftest.py` (uvicorn in a thread with temp `FA_HOME` and the builder patched to `_vf`), `tests/e2e/test_ui.py`
- Build output: `services/console/fa_console/static/`

**Interfaces:**
- **`api.ts`:** typed `get/post/upload`, an `sse(jobId, onEvent)` helper, and `ApiError{status, message}`.
- **`router.ts`:** hash routes:
  - `#/farms`, `#/farms/new`, `#/today/:fid`, `#/system`;
  - `#/f/:fid/{mission|twin|drone|simulate|iterate|plan|live|records}`.
- **Design:** load the `frontend-design` skill first. Tokens go on `:root` with dark mode. Touch targets ≥ 44 px.

- [ ] **Step 1:** e2e test `test_create_farm_wizard_builds` (fill the wizard → the job progress bar reaches done → the farm card shows "ready").
- [ ] **Step 2:** Run `FA_E2E=1 pytest tests/e2e -q`. Expected: FAIL.
- [ ] **Step 3:** Scaffold, `npm install` (proxy env), implement, `npm run build`.
- [ ] **Step 4:** Run `npm run typecheck && npm run build && FA_E2E=1 pytest tests/e2e -q`. Expected: PASS, with a screenshot in `runs/console_e2e/`.
- [ ] **Step 5:** Commit `feat(ui): console scaffold, farms, new-farm wizard`.

### Task 12: 3D digital farm

**Files:**
- Create: `apps/console/src/three/FarmScene.ts`, `three/layers.ts`, `pages/Twin3D.tsx`, `components/TimeSlider.tsx`, `components/CellInspector.tsx`

**Interfaces:**
- **`FarmScene`:**
  - `new FarmScene(canvas, scene: ScenePayload)`;
  - `setLayer(name)`, `setSnapshot(day, arrays)`, `setExaggeration(k)`;
  - `onPick(cb(cellIndex))`;
  - `addMarkers(kind, items)`, `showRoute(track)`;
  - `dispose()`.
- **Rendering:**
  - canopy is an `InstancedMesh` per cell (height from LAI, colour from the layer palette);
  - terrain is a `PlaneGeometry` displaced by `heights`;
  - ground is the S2 texture when present.

- [ ] **Step 1:** e2e `test_twin3d_renders_and_picks` (canvas non-blank via pixel sampling; clicking the centre opens the inspector; no console errors).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Typecheck, build, e2e. Expected: PASS, with a screenshot.
- [ ] **Step 5:** Commit `feat(ui): 3D digital farm with layers, markers, time slider`.

### Task 13: Drone twin 3D, mission planner, playback

**Files:**
- Create: `apps/console/src/three/DroneModel.ts`, `three/Playback.ts`, `pages/Drone.tsx`, `components/Gauges.tsx`

**Interfaces:**
- **`buildDrone(spec) -> THREE.Group`** at spec scale (metres). Rotor objects are exposed for spinning; nozzle emitters for plumes.
- **`Playback(scene, drone, track)`:** `play()`, `pause()`, `seek(t)`, `speed(x)`, `onFrame(cb(frame))`.

- [ ] **Step 1:** e2e `test_drone_playback` (simulate → gauges change over 2 s of playback → the KPI cards show sorties ≥ 1).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Typecheck, build, e2e. Expected: PASS, with a screenshot.
- [ ] **Step 5:** Commit `feat(ui): K3-A Pro 3D twin, mission planner and playback`.

### Task 14: Mission console and Simulate pages

**Files:**
- Create: `apps/console/src/pages/Mission.tsx`, `pages/Simulate.tsx`, `components/OpsQueue.tsx`, `components/Timeline.tsx`, `charts/*.ts`
- Load the `dataviz` skill first.

- [ ] **Step 1:** e2e `test_mission_step_approve_fly` (step to decision → approve → run → the flight appears in the timeline) and `test_simulate_charts_render`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Typecheck, build, e2e. Expected: PASS, with screenshots.
- [ ] **Step 5:** Commit `feat(ui): mission console and season charts`.

### Task 15: Iterate and Plan pages

**Files:**
- Create: `apps/console/src/pages/Iterate.tsx`, `pages/Plan.tsx`

- [ ] **Step 1:** e2e `test_iterate_to_approved_plan_exports` (the fixture perfect result is injected via the store → "Use this plan" → approve → the exports download with 200).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Typecheck, build, e2e. Expected: PASS.
- [ ] **Step 5:** Commit `feat(ui): iterations and final plan with timings and exports`.

### Task 16: Live, Records, System, Today, PWA

**Files:**
- Create: `apps/console/src/pages/{Live,Records,System,Today}.tsx`, `public/manifest.webmanifest`, `public/sw.js`, icons

- [ ] **Step 1:** e2e tests:
  - `test_today_language_switch` (en → hi shows Devanagari);
  - `test_live_page_handles_unavailable` (offline banner, no crash);
  - `test_system_shows_worker_token_and_sources`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement it. The service worker caches the shell and the last GET `/api/*` (network-first).
- [ ] **Step 4:** Typecheck, build, e2e. Expected: PASS.
- [ ] **Step 5:** Commit `feat(ui): live, records, system, farmer Today, PWA`.

### Task 17: Remote GPU worker

**Files:**
- Create: `services/console/fa_console/worker.py`, `scripts/console/Connect-GPU-Server.ps1`, `scripts/console/Connect-GPU-Server.cmd`
- Test: `tests/test_console_worker.py`

**Interfaces:**
- **`worker.main(argv)`:** `--server --token --kinds perfect,season,detect --once`.
  - It claims a job, downloads `farm.pkl`, runs the registered job function and completes it.
- **Connect script:** one `ssh` command: `-R 18099:127.0.0.1:8099`, the code bundle piped over stdin, and the worker started remotely. The user types the password.

- [ ] **Step 1:** `test_worker_claims_and_completes_quick_job` (a uvicorn thread plus `worker.main([... "--once"])` on a test job kind).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(console): remote GPU worker and connect script`.

### Task 18: Packaging, docs, full verification, delivery

**Files:**
- Create:
  - `scripts/console/Start-FutureAgri.cmd` and `.ps1`;
  - `deploy/console.Dockerfile`;
  - `docs/console.md`;
  - `docs/adr/0017-mission-console.md`.
- Modify: `docker-compose.yml`, `Makefile` (`console`, `console-build`, `e2e`), `README.md`

- [ ] **Step 1:** Run `pytest -q` (everything) and `FA_E2E=1 pytest tests/e2e -q`. Expected: all pass.
- [ ] **Step 2:** Run `python -m fa_console` and smoke-test it with curl (`/api/health` 200, `/` 200).
- [ ] **Step 3:** Package `futureagri_console.zip` (repo, built SPA, data cache) and deliver it to the laptop. Send screenshots.
- [ ] **Step 4:** Commit `docs+deploy: console launcher, Docker, operator guide, ADR 0017`.
