# 0016 — A digital twin of the 10 L K3-A Pro sprayer, calibrated to its spec sheet

**Status:** accepted · **Date:** 2026-10-31

## Context

The drone that flies the plan is a 10 L, four-rotor, 1300 mm sprayer with a JIYI
K3-A Pro flight controller. The research found the K3-A Pro is closed: no SDK, no
MAVLink, no SITL. It imports KML polygons into Agri Assistant (one file, polygons
only, ≤ 400 waypoints per group) and exports job records in an undocumented format.
Its spec sheet is inconsistent in places (pump 3.5 vs 5.5 L/min, swath 3–5 m vs
4 m, battery configuration unstated).

## Decision

A physics + behaviour emulator (`services/drone/fa_drone`), not a firmware clone:

* **Spec as data** (`data/t1_10l.yaml`), inconsistencies kept as ranges; battery
  inferred as 2 × 6S 16 Ah in series (~710 Wh, ~4 kg) — the only configuration
  consistent with 14 kg and 24 kg hover points.
* **Power**: momentum theory with one overall efficiency fitted to both hover
  points: η = 0.507 (figure of merit 0.60 with an assumed 0.85 drivetrain);
  predicted hover 12.3 min @ 24 kg and 27.3 min @ 14 kg against the sheet's 12 and
  28 (2–3 %). Forward flight adds Glauert induced velocity, profile and parasitic
  power (drag area 0.45 m², assumption); the bucket is real — 5–7 m/s needs 7–11 %
  less power than hover.
* **Battery**: equivalent circuit (OCV(SOC), R0, one RC pair), temperature and
  ageing derating; the return threshold (3.52 V/cell under load) is calibrated so
  the pack delivers the sheet's usable energy at the heavy hover point.
* **Spray**: JIYI linkage flow (Q ∝ speed, pump cap, off below 0.5 m/s), four
  nozzle plumes spreading with height, wind shift, a drift fraction, and
  **turbulent pass-to-pass cross-wind** (a steady wind shifts every pass alike and
  cannot stripe a field; gusts do). Effective swath (CV ≤ 25 %) 3.8 / 5.2 / 6.6 m at
  1.5 / 2.5 / 3.5 m — inside the 4.5–7.9 m measured for comparable drones.
  Coefficients are labelled for an ISO 24253-style calibration.
* **GNSS**: Gauss–Markov cross-track error (±2 m class; RTK centimetre).
* **K3-A Pro behaviour**: app-default pass angle (long edge), serpentine passes
  via the existing coverage planner, polygon routes for spot treatment, route
  groups split at 400 waypoints, speed/tilt/vertical limits, return on empty tank
  or battery threshold, refill + battery swap with pack charging logistics,
  breakpoint resume.
* **Mission simulator**: time-stepped flight → per-cell as-applied dose along the
  *actual* track → KPIs (time, energy, Wh/ha, sorties, swaps, refills, litres, CV,
  skipped and over-dosed area, off-target fraction, ha/h).
* **Integration**: KML export with vertex-limit simplification and exclusion
  polygons, a settings sheet for the app (height, speed, row spacing, dose per
  ha / mu / acre, flow mode, regulation pack), and tolerant job-record import
  that reports every malformed row.

Coverage feeds agronomy: the truth receives the deposited dose per cell and
efficacy follows a saturating dose–response (`fa_twin.damage.dose_efficacy`), so
GNSS error, wind and spacing choices change disease and weed outcomes.

## Consequences

* The "perfect run" can choose height, speed, spacing, carrier rate and RTK on
  money per hectare, and the choice is traceable to the spec sheet.
* Until flight logs and a collector test exist, drag area, battery resistance,
  spray-pattern coefficients and turbulence intensity are assumptions; the
  report lists them.
* Closed-loop *control* of the aircraft is impossible with this controller; an
  open autopilot (PX4/ArduPilot agricultural build) is the upgrade path if that
  is ever needed.
