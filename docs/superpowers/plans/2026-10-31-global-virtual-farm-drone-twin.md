# Global Virtual Farm + Spray-Drone Twin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a real-data virtual test farm anywhere on Earth, scan it with a simulated camera drone, run the twin with real archived ensemble forecasts, fly the chosen plan with a calibrated twin of the 10 L K3-A Pro sprayer, and search for the best plan the drone can fly.

**Architecture:** Four new services beside the existing twin — `services/farm/fa_farm` (sources + archetypes + ground truth), `services/scan/fa_scan`, `services/drone/fa_drone`, `services/runner/fa_runner` — with the truth/observation boundary enforced by types (`GroundTruthFarm` never reaches the twin; only `DigitalFarm`, forecasts and as-applied records do).

**Tech Stack:** Python 3.13, PCSE 6.0.13, numpy/scipy, shapely/pyproj, rasterio (GDAL /vsicurl), xarray + zarr + icechunk, pyarrow/fsspec, OR-Tools, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-10-31-global-virtual-farm-drone-twin-design.md`

## Global Constraints

- Global product: no India-specific defaults in new code; currency, units, prices and rules come from `regions/*.yaml`.
- Every externally sourced value carries a `Provenance(source, url, licence, retrieved, attribution)`.
- Licences: FTW CC-BY-4.0; SoilGrids CC-BY-4.0; Copernicus DEM attribution; ECMWF/NOAA via dynamical.org CC-BY-4.0; Open-Meteo flagged `commercial_use=False`.
- Drone spec values exactly as the user's sheet: tank 10 L, 4 nozzles, wheelbase 1300 mm, empty 10 kg, standard 24 kg, MTOW 28 kg, hover 12 min @ 24 kg and 28 min @ 14 kg, swath 3–5 m (4 m table), pump 3.5 L/min (brochure) / 5.5 L/min (BOM), operating speed ≤ 7 m/s, max 10 m/s, link 1000 m, −10…40 °C, props 34.7 in, pump cut-off < 0.5 m/s.
- K3-A Pro: hover accuracy ±2 m H / ±0.8 m V without RTK, tilt ≤ 30°, vertical ≤ 6 m/s, ≤ 400 waypoints per KML group, polygons only.
- Effective swath definition: widest pass spacing with CV ≤ 25 %.
- Tests run offline (recorded fixtures); network tests marked `@pytest.mark.network` and skipped unless `FA_NETWORK=1`.
- The 122 existing tests stay green.

## Review Focus

1. **Tiny or odd fields** (< 0.05 ha, or narrower than one pass) → a valid one-pass mission or an explicit refusal, never a crash. Test in Task 7.
2. **No forecast archive for the date** (before 2024-04 for IFS ENS) → GEFS, then climatology, with the fallback named in the report. Test in Task 2.
3. **Southern-hemisphere season across New Year** (Brazil soy Oct → Feb) → weather windows and day indexing correct. Test in Task 5.
4. **Wind above the limit on every day of the window** → "no feasible spray window" reported, no mission flown. Test in Task 11.
5. **Network down with a partial cache** → failure names the missing layer; no silent synthetic substitution. Test in Task 1.

---

### Task 1: Provenance, cache and HTTP plumbing

**Files:** Create `services/farm/fa_farm/__init__.py`, `services/farm/fa_farm/sources/__init__.py`, `services/farm/fa_farm/sources/base.py`; Test `tests/test_farm_sources.py`

**Interfaces — Produces:**
- `@dataclass(frozen=True) Provenance(source: str, url: str, licence: str, retrieved: str, attribution: str, commercial_use: bool = True)` with `.as_dict()`.
- `class Cache(root: Path)`: `key(*parts) -> str` (sha256), `get_json(key) -> dict | None`, `put_json(key, obj)`, `get_bytes/put_bytes`.
- `class DataUnavailable(RuntimeError)` with attribute `layer: str`.
- `fetch_json(url, params, cache, layer, timeout=60) -> dict` (cache first; on network error with no cache raises `DataUnavailable(layer)`).
- `gdal_env() -> dict` (proxy + CA bundle for /vsicurl).

- [ ] Step 1: tests — `test_cache_roundtrip`, `test_fetch_json_uses_cache_offline` (monkeypatch requests to raise; cached value returned), `test_fetch_json_missing_names_layer` (raises `DataUnavailable` with `.layer == "soil"`).
- [ ] Step 2: run → fail. Step 3: implement. Step 4: run → pass. Step 5: commit.

### Task 2: Weather history and real forecast ensembles

**Files:** Create `services/farm/fa_farm/sources/weather.py`, `services/farm/fa_farm/sources/forecasts.py`; fixtures `tests/fixtures/farm/power_*.json`, `tests/fixtures/farm/ens_*.npz`; Test `tests/test_farm_weather.py`

**Interfaces — Produces:**
- `power_daily(lat, lon, start: date, end: date, cache) -> tuple[list[dict], Provenance]` — PCSE-style records `DAY, TMAX, TMIN, RAIN(mm), IRRAD(J/m²/d), VAP(hPa), WIND(m/s), E0, ES0, ET0` (cm/d, via PCSE `reference_ET`), plus `RH` and `WIND` raw.
- `monthly_climatology(records) -> dict[int, dict]` (mean TMAX/TMIN/RAIN per month).
- `ForecastSource` enum: `IFS_ENS`, `AIFS_ENS`, `GEFS`, `CLIMATOLOGY`.
- `available_sources(issue: date) -> list[ForecastSource]` (IFS from 2024-04-01, AIFS from 2025-07-02, GEFS from 2020-10-01).
- `ensemble_forecast(lat, lon, issue: date, cache, days=15, sources=None, history=None) -> tuple[list[list[dict]], ForecastSource, Provenance]` — daily member records, bias-corrected by `bias_correct(members, climatology)`; falls back down the list; `CLIMATOLOGY` uses resampled history years.
- `to_pcse_provider(records, lat, lon, elev)` reusing `fa_twin.weather.records_provider`.

- [ ] Step 1: tests — `test_power_parse_fixture` (units: IRRAD J/m²/d > 1e6 in summer; VAP hPa 5–40), `test_available_sources_dates` (`date(2024,3,1)` → no IFS; `date(2025,8,1)` → IFS, AIFS, GEFS), `test_fallback_named_when_archive_missing` (issue 2019-06-01 → source `CLIMATOLOGY`, provenance says so) [Review Focus 2], `test_bias_correct_shifts_mean` (members 2 °C warm vs climatology → corrected mean within 0.1 °C).
- [ ] Steps 2–5 as Task 1. Network test `test_ifs_ens_point_read` marked `network`.

### Task 3: Soil, terrain, boundaries, satellite

**Files:** Create `services/farm/fa_farm/sources/soil.py`, `terrain.py`, `boundaries.py`, `satellite.py`; Test `tests/test_farm_layers.py`

**Interfaces — Produces:**
- `saxton_rawls(sand: float, clay: float, om_pct: float) -> dict` keys `SMW, SMFCF, SM0, KSAT_mm_h` (2006 equations, fractions in, volumetric out).
- `soilgrids_profile(lat, lon, cache) -> tuple[dict, Provenance]` — depth-weighted sand/clay/SOC/bdod for 0–60 cm and `soil_params: fa_twin.engine.SoilParams`.
- `dem_window(lat, lon, half_size_m, cache) -> tuple[np.ndarray, transform, Provenance]`; `slope_deg(dem, res)`, `twi(dem, res)`.
- `field_near(lat, lon, cache, min_ha, max_ha, min_conf=69) -> tuple[shapely.Polygon (WGS84), dict attrs, Provenance]` (FTW GeoParquet, bbox row-group filter, `trust_env=True`).
- `s2_cover_series(polygon, start, end, cache, max_cloud=60) -> tuple[list[tuple[date, float, float]], Provenance]` — (date, NDVI mean, FVC) with SCL mask; FVC = ((NDVI−0.15)/(0.90−0.15))² clipped.

- [ ] Step 1: tests — `test_saxton_rawls_loam` (sand .40 clay .20 OM 2.5 → SMFCF 0.27±0.02, SMW 0.13±0.02, SM0 0.44±0.03), `test_twi_higher_in_valley` (synthetic V-shaped DEM), `test_fvc_bounds`, `test_field_near_fixture` (recorded parquet slice → polygon area within limits). Network tests for each source.
- [ ] Steps 2–5.

### Task 4: Archetypes and region packs

**Files:** Create `services/farm/fa_farm/data/archetypes.yaml`, `services/farm/fa_farm/data/regions/{US,EU,BR,JP,CN,KR,AU,CA,KE,TH,VN,GLOBAL}.yaml`, `services/farm/fa_farm/archetypes.py`, `services/farm/fa_farm/regions.py`; Test `tests/test_archetypes.py`

**Interfaces — Produces:**
- `Archetype` dataclass: `id, name, locations: list[Site(name, lat, lon, country)], field_ha: (lo, hi), crop, variety, sowing: "MM-DD", harvest: "MM-DD", season_year_offset, irrigated, diseases: list[str], weeds: bool, region: str, drone_use, sources: list[str], supported: bool, unsupported_reason: str`.
- `load_archetypes() -> dict[str, Archetype]`; `season_dates(arch, year) -> (sow: date, harvest: date)` (harvest in next year when month < sowing month).
- `RegionPack`: `id, currency, area_unit, prices: dict, costs: dict, rules: Rules(wind_max_ms, buffer_m, max_height_m, max_speed_ms, weight_threshold_kg, records: list[str], aerial_default_allowed: bool, verified: bool, notes)`; `load_region(id) -> RegionPack`.

- [ ] Step 1: tests — all 8 archetypes load; A7/A8 `supported is False` with reasons; `season_dates(A3, 2024)` → sow 2024-10-xx, harvest 2025-02-xx [Review Focus 3]; `load_region("BR").rules.buffer_m == 20`; `load_region("EU").rules.aerial_default_allowed is False`; `load_region("US").rules.weight_threshold_kg == 24.9`; unverified packs have `verified False`.
- [ ] Steps 2–5.

### Task 5: Ground-truth farm

**Files:** Create `services/farm/fa_farm/grid.py`, `heterogeneity.py`, `truth.py`, `anchor.py`, `build.py`; Test `tests/test_truth.py`

**Interfaces — Consumes:** Tasks 1–4; `fa_twin.engine.ZoneConfig, ZoneTwin, SoilParams`; `fa_twin.damage.Blight, BlightParams, weed_loss_frac, infection_day`.
**Produces:**
- `FarmGrid(polygon_utm, crs, cell_m)`: `.cells` (n,2 centres), `.mask`, `.shape`, `.area_ha`, `cells_in(geom) -> np.ndarray[bool]`.
- `soil_zones(grid, base: SoilParams, twi, rng, k=4) -> (zone_of_cell: int array, zone_soils: list[SoilParams])`; `emergence_field(grid, rng, cv)`; `weed_seedbank(grid, rng, mean_m2, clusters_per_ha, radius_m) -> density array`.
- `GroundTruthFarm`: built by `build_truth(spec: FarmSpec, weather_records, rng) -> GroundTruthFarm` with `FarmSpec(archetype, site, year, polygon, soil, dem, s2_series, region, cell_m=5.0, seed)`; methods `state(day) -> CellState(lai, cc, dvs, weeds_m2, blight_sev)`, `apply(as_applied: AsApplied)`, `harvest() -> HarvestResult(yield_kg_ha_cells, field_yield_kg_ha, losses)`.
- `AsApplied(day, product: "fungicide"|"herbicide", dose_frac: np.ndarray per cell (deposited/label), off_target_l)`.
- `anchor_to_s2(cfg, weather, s2_series, members=24) -> dict param overrides` (EnKF parameter fit on FVC).

- [ ] Step 1: tests — `test_grid_area_matches_polygon` (±2 %), `test_zones_partition_cells`, `test_weed_clusters_patchy` (variance/mean > 2), `test_truth_season_runs` (A5-like synthetic weather: field yield > 0, cells vary), `test_herbicide_dose_reduces_weeds_only_where_applied`, `test_underdosed_strip_keeps_blight` (dose 0 on a strip → higher severity there), `test_southern_hemisphere_dates` (Oct→Feb season runs end to end) [Review Focus 3], `test_anchor_moves_toward_s2` (synthetic S2 from a known parameter set → fitted TSUM1 closer to truth than prior).
- [ ] Steps 2–5.

### Task 6: Drone spec, power and battery

**Files:** Create `services/drone/fa_drone/__init__.py`, `spec.py`, `data/t1_10l.yaml`, `power.py`, `battery.py`; Test `tests/test_drone_physics.py`

**Interfaces — Produces:**
- `DroneSpec` (fields from Global Constraints + `rotor_diameter_m=0.881, n_rotors=4, battery_wh_nominal=710.4, usable_frac=0.85, pump_lpm: (3.5, 5.5), swath_m: (3.0, 5.0)`), `load_spec(name="t1_10l") -> DroneSpec`.
- `PowerModel.calibrate(spec) -> PowerModel` (fits η and exponent from the two hover points); `.hover_w(mass_kg)`, `.forward_w(mass_kg, v_ms, wind_ms=0)`, `.endurance_min(mass_kg)`.
- `Battery(spec, temp_c=25, cycles=0)`: `.draw(power_w, dt_s) -> voltage`, `.soc`, `.usable_wh_left`, `.low_voltage()`.

- [ ] Step 1: tests — `test_hover_points_within_5pct` (endurance(24)=12±0.6 min, endurance(14)=28±1.4 min), `test_eta_physical` (0.3 < η < 0.8), `test_forward_power_bucket` (P(5 m/s) < P(0) < P(12 m/s)), `test_cold_battery_less_energy` (−10 °C usable < 25 °C usable), `test_low_voltage_triggers_before_empty`.
- [ ] Steps 2–5.

### Task 7: Spray, GNSS, K3-A Pro emulator, mission simulator

**Files:** Create `services/drone/fa_drone/spray.py`, `gnss.py`, `k3a.py`, `mission.py`; Test `tests/test_drone_mission.py`

**Interfaces — Consumes:** Task 5 `FarmGrid`, `AsApplied`; Task 6.
**Produces:**
- `SprayModel(spec, nozzle_offsets_m, sigma0=0.35, sigma_per_m=0.45, drift_k=0.06)`: `.flow_lpm(v, rate_l_ha, swath_m, pump_lpm)` (linkage, 0 below 0.5 m/s, capped), `.lateral_profile(y, height_m, wind_cross_ms)`, `.effective_swath(height_m, wind_ms, cv_max=0.25) -> float`, `.off_target_frac(height_m, wind_ms, v)`.
- `GnssError(rtk: bool, sigma_h=1.0, tau_s=60, seed)` with `.step(dt) -> (dx, dy)` (±2 m ≈ 2σ).
- `MissionSettings(height_m=2.5, speed_ms=5.0, swath_m=4.0, rate_l_ha=15.0, rtk=False, pump_lpm=3.5, route_angle_deg=None)`.
- `generate_route(grid, polygon, settings, targets: np.ndarray[bool] | None) -> Route(passes, n_waypoints)`; raises `RouteTooLarge` if > 400 waypoints per group (splits into groups instead when allowed).
- `simulate_mission(grid, polygon, home_xy, spec, settings, weather_hour: dict(wind_ms, wind_dir_deg, temp_c), targets=None, seed=0) -> MissionResult(as_applied_dose_l_ha: np.ndarray, kpis: dict, telemetry: list[dict], sorties: int, refills: int, batteries: int, feasible: bool, reasons: list[str])`.
- KPIs keys: `time_min, energy_wh, wh_per_ha, litres, sorties, battery_swaps, refills, cv_pct, skipped_frac, overdosed_frac, off_target_frac, area_ha`.

- [ ] Step 1: tests — `test_flow_linkage` (5 m/s, 15 L/ha, 4 m → 1.8 L/min; 0.4 m/s → 0; 7 m/s 25 L/ha capped at pump), `test_effective_swath_reasonable` (2.5 m height calm → 3–7 m), `test_cv_hand_case` (uniform profile → CV 0), `test_rtk_reduces_skips` (same field: skipped_frac RTK < no-RTK), `test_tank_forces_refills` (2 ha at 15 L/ha → ≥ 3 refills), `test_tiny_field_one_pass_or_refusal` (0.03 ha strip 3 m wide → feasible with ≥ 1 pass or `feasible False` with reason) [Review Focus 1], `test_wind_over_limit_infeasible` (wind 9 m/s > level-4 gust limit → `feasible False`).
- [ ] Steps 2–5.

### Task 8: JIYI I/O

**Files:** Create `services/drone/fa_drone/jiyi_io.py`; Test `tests/test_jiyi_io.py`

**Interfaces — Produces:** `export_kml(polygon_wgs84, exclusions, name, path) -> Path` (polygons only, closed rings, ≤ 400 vertices per polygon else simplify with tolerance and report), `settings_sheet(settings: MissionSettings, spec, region) -> dict`, `import_job_records(path_or_text, mapping=None) -> list[JobRecord(start, flight_min, area_ha, dose_l_per_area, total_l, fc_serial, distance_m, crop)]` (CSV or JSON; unknown columns kept; validation errors listed, never silently dropped).

- [ ] Step 1: tests — KML parses with xml.etree, one Polygon per field, ring closed; 1,000-vertex polygon simplified to ≤ 400; job-record CSV with mu units converted to ha; malformed row reported in `errors`.
- [ ] Steps 2–5.

### Task 9: Scan simulator and DigitalFarm

**Files:** Create `services/scan/fa_scan/__init__.py`, `survey.py`, `measure.py`, `digital.py`, `data/detector_wg_medium.json` (per-class recall/precision derived from `bench/results/gates_wg_medium.json` and cached predictions); Test `tests/test_scan.py`

**Interfaces — Consumes:** `GroundTruthFarm.state(day)`, `fa_core.geo.CAMERAS`.
**Produces:** `plan_scan(polygon, camera_key, gsd_m, front=0.8, side=0.7) -> ScanPlan(altitude_m, frames, flight_min, batteries)`; `simulate_scan(truth, day, plan, rng) -> ScanObservation(cc_cells, dvs_cells, weed_detect_m2, blight_detect, gsd_m)` with error sds recorded; `build_digital_farm(obs, grid, zones) -> DigitalFarm(zones: list[ZoneObs(zone_id, cc, cc_sd, dvs, dvs_sd, weeds_m2, weeds_sd, area_ha)], weed_map, blight_seen: bool, provenance)`.

- [ ] Step 1: tests — altitude for 10 mm GSD on `p4rtk` within 1 m of geometry; coarser GSD lowers weed recall; DigitalFarm has no attribute reaching `GroundTruthFarm` (separation test: `pickle` size and type check); zone CC means within 2 sd of truth.
- [ ] Steps 2–5.

### Task 10: Multi-zone twin and generalised economics

**Files:** Create `services/runner/fa_runner/__init__.py`, `twinfarm.py`, `economics.py`; Modify `services/twin/fa_twin/damage.py` (add `dose_efficacy(dose_frac, emax, k)`); Test `tests/test_twinfarm.py`

**Interfaces — Produces:** `Economics.from_region(pack, crop) -> Economics(price_per_kg, fungicide_cost_ha, herbicide_cost_ha, drone_cost_ha, scan_cost_ha, currency)`; `TwinFarm(digital: DigitalFarm, archetype, region, history, settings)` wrapping one `FieldTwin` per zone: `ingest(digital)`, `advance_to(day)`, `forecast_yields(day, members) -> np.ndarray`, `fungicide_need(day, forecast) -> dict zone→p_spray_better`, `weed_targets(threshold_m2) -> np.ndarray[bool]`.

- [ ] Step 1: tests — `dose_efficacy(1.0)` ≈ emax, `dose_efficacy(0)` = 0, monotone; TwinFarm zones match DigitalFarm zones; region economics carry currency.
- [ ] Steps 2–5.

### Task 11: Runner — scenarios, plans, perfect run, closed loop, CLI

**Files:** Create `services/runner/fa_runner/scenarios.py`, `plans.py`, `optimise.py`, `season.py`, `report.py`, `cli.py`, `__main__.py`; Test `tests/test_runner.py`

**Interfaces — Produces:** `FlightGrid` of `MissionSettings` candidates; `optimise_flight(grid, polygon, spec, region, wind_scenarios, targets) -> list[RankedSettings(settings, score, kpis_mean, kpis_p90, feasible_frac)]`; `run_season(farm_spec, policy: "twin"|"calendar"|"none", seed, forecast_sources) -> SeasonResult(net, yield, sprays, litres, sorties, drift, cv, log, as_applied)`; `perfect_run(farm_spec, n_scenarios, lam=0.5) -> PerfectRun(plan, ranked, metrics, provenance)`; `write_report(result, out_dir) -> dict paths (json, csv, geojson)`; CLI subcommands `build-farm, scan, fly, season, optimise, export-kml`.

- [ ] Step 1: tests — flight optimiser never returns infeasible settings as best; `test_no_window_when_always_windy` (all forecast days wind > limit → plan says no feasible window, zero missions) [Review Focus 4]; twin policy uses fewer litres than calendar on a no-disease season; report files exist and JSON has `attribution` list; CLI `--help` exits 0.
- [ ] Steps 2–5.

### Task 12: API, docs, benchmarks

**Files:** Create `services/api/fa_api/farm_routes.py`, `bench/archetype_runs.py`, `docs/adr/0015-global-real-data-virtual-farm.md`, `docs/adr/0016-spray-drone-twin.md`; Modify `services/api/fa_api/main.py`, `Makefile` (deliver as `Makefile.new`), `README.md`; Test `tests/test_farm_api.py`

- [ ] Endpoints `POST /farm/build`, `POST /farm/{id}/scan`, `POST /farm/{id}/optimise`, `POST /drone/simulate`, `POST /drone/kml`, `POST /drone/job-records`; tests with fixtures; benchmark runs A5 (NL potato 2025), A4 (Iowa maize 2025), A3 (Sorriso soy 2024/25), A6 (Kakamega maize 2025), A1 (Niigata rice 2025, limited-fidelity flag) and writes `bench/results/archetypes.json`.

### Task 13: H200 jobs

**Files:** Create `ml/h200/gsd_sweep.py`, `ml/h200/run_gsd_sweep.sh`, laptop `Run-H200-Twin.cmd` + `run-h200-twin.ps1`.

- [ ] GSD sweep (WeedsGalore test tiles at 2.5/5/10/20 mm → per-class recall/precision JSON consumed by Task 9), ONNX parity rerun, batch archetype runs with `python -m fa_runner optimise --scenarios 200`. Requires the user to type the H200 password.

### Task 14: Verification and delivery

- [ ] Full test suite; benchmarks; fresh-eyes review of the branch; write back to the laptop; final summary with sources and attribution.
