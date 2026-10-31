# 0013 — Post-audit rebuild: realistic weather, a filter that works, a weekly loop

**Status:** accepted · **Date:** 2026-10-31 · **replaces the headline numbers on
the Drones 2026 poster and in the September README**

## Context

The 30 Oct 2026 simulation audit (`../audit/AUDIT_RESULTS.md`, outside the repo)
reran every section on realistic Bennett weather and found the repo's headline
numbers rested on:

* a sine-wave weather generator giving **102 mm of rain a year** (Delhi ~775 mm);
* an OSSE that used **16 weekly observations**, not the one pass the design
  claims;
* a conformal review rate measured on a **stand-in classifier**.

It also found what the twin is actually worth in simulation: irrigation
scheduling (−44 % water at equal yield) and forecast-driven fungicide timing
(−26 % sprays), with weekly re-planning mandatory. Reviewing the code after the
audit turned up two filter bugs the audit had not caught.

## Decisions and what was measured

### 1. Weather: normals-calibrated generator everywhere
`fa_twin.weather.generate_records` — Markov-chain wet days, gamma amounts,
AR(1) temperatures, season-level anomalies — calibrated to IMD Safdarjung
1991–2020 normals (Tmax/Tmin/rain/rainy days checked against the published
table; radiation, vapour pressure and wind approximate). Mean of 12 decades:
**755 mm/yr (normal 774), JJAS 626 mm (640)**, daily rain capped at 200 mm.
`make_synthetic_weather` keeps its name and NOT-FOR-ADVICE stamp, so every
caller moved at once. `forecast_ensemble` gives observed past + 15-day forecast
(error growing with lead) + climatology tail, seeded per member for common
random numbers. Pinned by `tests/test_weather.py`.

### 2. Assimilation: two bugs fixed
* **Parameter updates never reached the crop.** The EnKF wrote updated
  TSUM1/TSUM2/SPAN/RGRLAI into a bookkeeping dict only. They are now written
  into each member's running PCSE phenology and leaf-dynamics objects
  (`test_parameter_update_reaches_the_running_crop`).
* **Inflation was cancelled in the same line that applied it.** Now applied to
  the forecast ensemble before the analysis (`test_inflation_widens_the_posterior`).
* Parameters are log factors in the state; observation operators for **LAI,
  canopy cover (1 − e^(−0.6·LAI)) and growth stage (DVS)**, assimilated jointly;
  per-member weather supported.

### 3. One pass: what it buys (`bench/one_pass_osse.py`, 60 trials, 24 members)

| pass | 30-day canopy RMSE vs open loop (cover + stage) | better in | yield error |
|---|---|---|---|
| day 30 | **−13 %** | 33/60 | no improvement (24–40/60) |
| day 45 | **−48 %** | 42/60 | no improvement (26–34/60) |

The day-45 figure reproduces the audit (−49 %); the day-30 figure is much
smaller than the audit's −33 %. **Schedule the pass around day 45** for canopy.
Weed control in potato has to happen before earthing-up (~day 30), so one pass
cannot be optimal for both: either walk the weed stations earlier, or accept a
later herbicide call. The season simulator uses day 35 as the compromise.
No yield claim is made from one pass.

### 4. "Overconfident ensemble" was mostly a finite-ensemble artefact
Open-loop 80 % intervals covered **0.70 ± 0.06** with no assimilation at all.
`np.percentile(v, [10, 90])` on 24 exchangeable members is expected to cover
only ~0.74. Inflation (1.3, 1.6) changed nothing measurable.
`EnsembleTwin.interval()` chooses order statistics so (m − k)/(n + 1) ≥ level
(`test_finite_ensemble_interval_holds_its_level`: ≥ 0.79 vs < 0.77 for the
percentile interval). Default inflation stays 1.0.

### 5. Crop twin and damage layer
* Regional variety **wheat / IN_NW_timely**: Winter_wheat_102 with IDSL=0,
  TSUM1=1400, TSUM2=1000, provenance in every summary ("phenology only — NOT a
  yield calibration"). Passes the photoperiod gate; European wheat is still
  refused.
* `fa_twin.damage`: late blight (logistic epidemic, favourable-day rule,
  protectant with wash-off, daily LAI removal) and Cousens weed loss. Every
  coefficient is an assumption and says so.

### 5b. Nitrogen: wheat can be N-limited, the shipped potato set cannot
Build-plan step 3 asked for WOFOST 8.1 nitrogen runs to explain the gap between
the water-limited ceiling and farm yields (`bench/nitrogen_response.py`,
irrigated, 30 + 10 kg/ha soil N):

| crop | 0 N | 120 N | 240 N | gain 0→240 |
|---|---|---|---|---|
| wheat IN_NW_timely (grain t/ha) | 2.96 | 5.43 | 6.65 | +124 % |
| potato Potato_701 (fresh t/ha) | 54.8 | 56.1 | 57.5 | **+4.8 %** |

The potato crop with no fertiliser took up only 40 kg N/ha (soil N exhausted by
early December) and still yielded above the 7.2 ceiling: implied tuber N ~0.2 %
of dry matter vs ~1.2–1.5 % in real potatoes. **The shipped 8.1 potato set does
not represent nitrogen limitation**, so it cannot explain the yield gap or advise
on nitrogen. `check_nitrogen_response()` now refuses N advice when 200 kg N
gains < 10 % (potato refused, wheat passes — tested). The potato yield gap stays
represented by the farm factor (0.5 × ceiling) until a field calibration exists.

### 6. The weekly loop
* `fa_planner.irrigation` — soil-water trigger (55 % of available water), ≥ 5 d
  apart, skip if > 10 mm forecast in 2 days.
* `fa_planner.weekly` — fungicide spray-or-wait on a forecast ensemble with CRN,
  risk-neutral default, CVaR option, SOP spray windows (`fa_planner.sop`);
  herbicide none/blanket/spot from the weed map; inoculum-alert input.
* **Scouting flies only when it could change the decision** (planner undecided:
  spraying wins in 25–75 % of members) — routine weekly scouting lost money in
  the audit, and with a "risk this week" trigger alone the twin flew 7 flights
  in an uninfected season.
* **At least 10 forecast members.** With 4, an infected season got 4 sprays
  instead of 8 and lost 6.6 % yield.
* `fa_loop.orchestrator.FieldTwin` runs the same code operationally (API) and
  against a simulated truth (`fa_loop.season`).

### 7. Router: tank capacity and spot spraying
Liquid is a node demand capped by the tank per sortie; spot mode scales liquid
by each swath's treated fraction and skips clean swaths. A 6 ha broadcast job
with a 10 L tank at 20 L/ha now needs ≥ 12 sorties (the audit caught the repo
planning it as 1). The independent verifier checks liquid as well as energy.

## What this does not change

Nothing has flown or been measured in a field. Blight, weed and price figures
are assumptions; no crop parameters are calibrated for India; the weather is a
generator, not station data.
