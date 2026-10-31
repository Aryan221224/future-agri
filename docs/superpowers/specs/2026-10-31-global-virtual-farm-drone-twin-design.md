# Global virtual test farm, scan simulator, spray-drone twin and iteration runner

**Date:** 2026-10-31 · **Status:** approved to build (user: "now build the full complete
project as I described", "use the full research above to build") · **Scope:** global, not
India-specific.

## 1. What this is for

Future Agri's loop is: a **separate camera drone scans the field** → the scan builds the
**digital farm** → the **twin runs many futures** and picks the plan → the **real spray
drone** (10 L quad, JIYI K3-A Pro flight controller) flies it → later scans and as-applied
records **close the loop**.

Before any real field, every link must be exercised against a farm whose truth is known.
This subsystem builds that farm from real data for any location on Earth, scans it with a
simulated camera drone, runs the twin with the **ensemble forecasts that were actually
issued** that season, flies the chosen plan with a **digital twin of this specific drone**,
and reports what happened against ground truth — then searches for the **best run the
drone can actually fly**.

Success = for each supported archetype farm: a reproducible season run on real data,
a ranked set of plans with net return (mean and CVaR), chemical, drift, coverage
uniformity, sorties, batteries and operator time, and a "perfect run" whose choice is
traceable to the drone's specs. Every number carries provenance; anything not modelled
is reported as not modelled.

## 2. Hard facts the design follows (research report, 31 Oct 2026)

1. Spray-drone work is a contractor business dominated by Asia; the 10 L class sits at
   the small end — smallholder, steep-slope, orchard-edge, spot-spray and research use.
2. **The K3-A Pro is closed**: no SDK, no MAVLink, no SITL. It imports **KML polygons**
   (one file, polygons only, ≤ 400 waypoints per group) into JIYI Agri Assistant and
   exports **job records** (start time, flight time, area, dose, distance, crop) in an
   undocumented format. So the drone twin is a calibrated physics + behaviour emulator,
   and the integration is KML out / job records in.
3. A twin is only real if validated against held-out truth: OSSE on virtual farms now,
   hindcasts on real fields later. Uncertainty is shown, never hidden.

## 3. Architecture

```
            ┌────────────────────── test harness only ──────────────────────┐
real data → │ fa_farm: GroundTruthFarm (boundary, soil, DEM, weather, S2,   │
            │          heterogeneity, per-zone WOFOST, weeds, disease)      │
            └──────────────┬──────────────────────────────▲─────────────────┘
                 scan obs  │                    as-applied │ dose raster
                           ▼                              │
 fa_scan: camera-drone survey → measurement model → DigitalFarm (zone maps + sd)
                           │
                           ▼
 twin (fa_twin + fa_loop): per-zone EnKF WOFOST + damage ── real ENS forecasts (fa_farm.sources.forecasts)
                           │
                           ▼
 fa_runner: candidate plans × scenarios → E[net] − λ·CVaR → plan; flight-parameter search
                           │
                           ▼
 fa_drone: K3-A Pro emulator + airframe physics → mission → as-applied raster, KPIs, KML
```

The **truth/observation separation** is enforced in code: the twin only receives
`DigitalFarm` objects, forecasts and as-applied records — never `GroundTruthFarm`.

## 4. Components

### 4.1 `fa_farm.sources` — real-data adapters (all cached on disk, offline-first)

| layer | source | access | licence |
|---|---|---|---|
| field boundary | FTW global predictions 2024–25 (PRUE) | GeoParquet over HTTPS, row-group bbox filter | CC-BY-4.0 |
| soil | SoilGrids 2.0, 250 m (sand, clay, SOC, bulk density; 4 depths) | COG/VRT via GDAL | CC-BY-4.0 |
| terrain | Copernicus GLO-30 DEM | COG on AWS | free incl. commercial, attribution |
| weather history | NASA POWER daily (AG) | REST | open |
| forecasts (real, archived) | ECMWF IFS ENS 0.25° 51 members (2024-04→), ECMWF AIFS ENS (2025-07→), NOAA GEFS 31 members (2020-10→), via dynamical.org | icechunk/Zarr on S3 | CC-BY-4.0 (+ ECMWF terms) |
| satellite | Sentinel-2 L2A (Earth Search STAC) | COG windows, SCL cloud mask | open |
| alt. weather | Open-Meteo ERA5 archive | REST | **non-commercial unless paid** — flagged |

Every adapter returns data plus a `Provenance` record (source, URL, licence, retrieved
at, attribution text). The cache is content-addressed; a run is reproducible offline once
its inputs are cached. Soil texture → WOFOST water parameters by **Saxton & Rawls (2006)**.
Forecast members are bias-corrected against the site's NASA POWER climatology (additive
for temperature, multiplicative for rain, by month).

### 4.2 `fa_farm` — archetypes, region packs, ground truth

* `archetypes.yaml`: the eight research archetypes (A1 East-Asian paddy … A8 Xinjiang
  cotton) with example locations, field-size priors, crop + calendar, pests/diseases,
  climate, drone-use pattern, and per-field source citations. Archetypes whose crop the
  model cannot represent (A7 tree crops, A8 cotton defoliation) load, but runs **refuse**
  with the reason.
* `regions/*.yaml`: currency, area unit, prices and costs, and a **regulation constraint
  pack** (wind limit, buffers, height/speed caps, weight-path rules, record fields,
  `verified: true/false`). Packs: US (Part 137/107, 55 lb rule), EU (default deny +
  derogation flag), BR (Portaria 298: 20 m buffers, weather in records), JP/CN/KR/AU/CA
  (unverified — the runner warns).
* `GroundTruthFarm`: field polygon in local UTM, grid of cells (default 5 m), soil zones
  from SoilGrids + a Gaussian random field + DEM wetness index, emergence variability,
  weed seedbank as a Neyman–Scott cluster process, disease inoculum and foci.
  Crop growth: WOFOST per soil zone with the true (observed) weather. **Anchored to the
  real field**: the zone parameters are fitted so the simulated canopy cover follows the
  field's Sentinel-2 cover series for that season (EnKF parameter estimation).
  Damage per cell: weeds by Cousens; late blight by the existing epidemic model split
  into protected/unprotected compartments by the as-applied dose. Yield per cell =
  zone yield × (1 − weed loss) × (1 − disease loss).

### 4.3 `fa_scan` — separate camera drone

* Survey plan from the camera (existing `fa_core.geo.CAMERAS`), target GSD, 75–80 %
  front / 65–70 % side overlap; flight time and battery count for the scouting drone.
* Measurement model, at the scan GSD: canopy cover and growth stage per cell with error
  standard deviations; weed detections from **the real RF-DETR-Medium WeedsGalore
  results** (per-class recall/precision at the operating threshold, 2.5 mm GSD), degraded
  with GSD by object size in pixels (labelled assumption until the H200 GSD sweep
  replaces it); disease detection probability vs severity and GSD (assumption, labelled).
* `DigitalFarm`: management zones with mean ± sd of canopy cover and DVS, a weed-density
  map with uncertainty, disease detections — the only thing the twin sees.

### 4.4 `fa_drone` — the spray-drone digital twin

* `spec`: the user's spec sheet as data, inconsistencies as ranges (pump 3.5 vs 5.5 L/min,
  swath 3–5 m, battery configuration inferred 12S 16 Ah ≈ 710 Wh).
* `power`: momentum theory P = (mg)^1.5 / (η √(2ρA)) with η fitted to both hover points
  (12 min @ 24 kg, 28 min @ 14 kg; target ≤ 5 % error on each), forward flight with
  induced (Glauert), profile and parasitic terms, pump and avionics power, mass falling
  as liquid is sprayed.
* `battery`: equivalent circuit (OCV(SOC), R0, one RC pair), temperature derating, ageing,
  low-voltage return threshold.
* `spray`: JIYI "linkage" flow Q = k·V capped by the pump, cut-off below 0.5 m/s; lateral
  deposition kernel from four nozzles with height-dependent spread and wind drift;
  effective swath = widest pass spacing with CV ≤ 25 %; off-target (drift) fraction.
  Kernel parameters are labelled for calibration by an ISO 24253-style test.
* `gnss`: Gauss–Markov cross-track error (±2 m / ±0.8 m without RTK; centimetre with RTK).
* `k3a`: K3-A Pro behaviour — route generation like Agri Assistant (parallel passes along
  the longest edge at the set spacing), speed gears, 30° tilt / 6 m/s vertical limits,
  breakpoint resume after refill or battery swap, return on low battery / low liquid /
  link loss beyond 1 km, ≤ 400 waypoints per group.
* `mission`: time-stepped execution → telemetry, as-applied dose raster on the farm grid,
  KPIs (time, energy, sorties, batteries, refills, litres, coverage CV, skipped and
  double-dosed area, off-target fraction, Wh/ha).
* `jiyi_io`: KML export the app accepts (field + exclusion polygons, waypoint-limit
  check) plus a settings sheet (height, speed, swath, dose per area, flow mode); job-
  record import with a field-mapping table and validation (the app is an untrusted
  boundary).

### 4.5 Twin and planner generalisation

`FieldTwin` runs per management zone (EnKF per zone). Economics, currency and units come
from the region pack instead of being hard-coded. Weed treatment becomes **spot spray**
on mapped cells; fungicide is per zone or blanket. Efficacy depends on the dose actually
deposited (dose-response), so GNSS error, wind and swath choice feed straight into
agronomic outcome.

### 4.6 `fa_runner` — future iterations and the "perfect run"

* Scenarios = real archived ENS members (days 1–15) + climatology tail × EnKF members ×
  perception draws × drone-parameter draws (pump, swath, GNSS).
* Decisions: scan date and GSD; spray / wait per zone and week (planner); flight
  parameters (height, speed, pass spacing, dose rate, RTK on/off); sortie plan.
* Objective: maximise E[net] − λ·CVaR₈₀(loss) subject to drone limits (energy, tank,
  wind, tilt), label rate and the region's regulation pack.
* Flight parameters by grid/Bayesian search with the mission simulator over wind
  scenarios; agronomic plan by the forecast-ensemble planner; the closed loop re-plans
  weekly with the forecast issued that week.
* Output per run: JSON + CSV + GeoJSON (as-applied, zones, weed map), a ranked plan
  table and the perfect-run summary. Parallel by process pool; the same CLI runs on the
  H200 server for large batches.

### 4.7 Product surface

CLI (`python -m fa_runner …`: build-farm, scan, plan, fly, season, optimise, export-kml),
FastAPI endpoints for the same, docs (ADR 0015 global data & farm, ADR 0016 drone twin),
`make` targets, and attribution text for every CC-BY source in each report.

## 5. Models and training (H200)

* Perception: RF-DETR (Apache-2.0) — keep RF-DETR-Medium; on the H200 add (a) a **GSD
  sweep**: evaluate it on WeedsGalore test tiles down-sampled to 5/10/20 mm to replace
  the assumed GSD curve with measured recall/precision, (b) RF-DETR-Seg fine-tune, (c) the
  ONNX parity rerun. AGPL models (YOLO26) are excluded from the commercial product.
* Weather: real ECMWF/NOAA ensembles (no training needed). Crop: PCSE-WOFOST + EnKF.
* WeedsGalore is one German maize field: per-region recalibration on field labels stays
  mandatory (ADR 0014).

## 6. Error handling

Network failures fall back to cache, then fail loudly with the missing layer named.
Missing forecast archive for a date → climatology members with a warning in the report.
Unsupported crop / disease → refusal with reason. Unverified regulation pack → warning in
every plan. Drone plans that violate energy, tank, wind or regulation limits are never
returned as feasible; the verifier re-checks every mission.

## 7. Testing

Unit tests on recorded fixtures (no network in CI); integration tests marked `network`;
drone calibration tests (both hover points within 5 %, flow and swath arithmetic, CV
computation against a hand-computed case); truth/observation separation test; closed-
loop OSSE test on a small virtual farm; all 122 existing tests stay green.

## 8. Out of scope (stated so nobody assumes it)

Running JIYI firmware; 3-D image rendering of the farm (measurement-level simulation is
used; image compositing is a later option); tree-crop and cotton crop models; legal
review of the unverified regulation packs; field validation (needs a real field).
