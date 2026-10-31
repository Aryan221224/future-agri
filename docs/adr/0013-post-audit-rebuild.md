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
* After the analysis each member is **re-run from sowing with its analysed
  parameters** (the audit prototype's approach), so a growth-stage observation
  corrects the current stage and history, not only future rates; writing SPAN in
  place would also kill leaf classes older than the new SPAN
  (`test_growth_stage_observation_corrects_the_current_stage`).

### 3. One pass: what it buys (`bench/one_pass_osse.py`, 60 trials, 24 members)

| pass | 30-day canopy RMSE vs open loop (cover + stage) | better in | yield error |
|---|---|---|---|
| day 30 | **−18 %** | 33/60 | no improvement (26/60 better) |
| day 45 | **−51 %** | 43/60 | no improvement (25/60 better) |

(Final run, members re-run from sowing after the analysis. Before that fix the
same benchmark gave −13 % and −48 %.) The day-45 figure reproduces the audit
(−49 %); the day-30 figure stays well below the audit's −33 %. **Schedule the
pass around day 45** for canopy.

The yield limit is the weather, not the filter: with the true weather and 16
weekly observations the same filter cuts yield error 82 % and canopy error 75 %
(16/16 trials, `bench/osse_assimilation.py`).
Weed control in potato has to happen before earthing-up (~day 30), so one pass
cannot be optimal for both: either walk the weed stations earlier, or accept a
later herbicide call. The season simulator uses day 35 as the compromise.
No yield claim is made from one pass.

### 4. "Overconfident ensemble" was mostly a finite-ensemble artefact
Open-loop 80 % intervals covered **0.70 ± 0.06** with no assimilation at all.
`np.percentile(v, [10, 90])` on 24 exchangeable members is expected to cover
only ~0.74. Inflation 1.3 changed nothing measurable (an exploratory run also
tried 1.6, with the same result). `EnsembleTwin.interval()` chooses order
statistics so (m − k)/(n + 1) ≥ level: on the twin itself (final one-pass run)
it covered **0.77–0.85** against 0.68–0.77 for the percentile interval
(SE ≈ 0.06), and in `test_finite_ensemble_interval_holds_its_level` ≥ 0.79 vs
< 0.77. Default inflation stays 1.0.

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
on nitrogen. `check_nitrogen_response()` refuses N advice when 200 kg N gains
< 10 % (potato refused, wheat passes — tested). No advice path offers nitrogen
yet; any future one (including the older `fa_planner.scheduler` N search) must
call this gate first. The potato yield gap stays
represented by the farm factor (0.5 × ceiling) until a field calibration exists.

### 6. The weekly loop
* `fa_planner.irrigation` — soil-water trigger (55 % of available water), ≥ 5 d
  apart, skip if > 10 mm forecast in 2 days.
* `fa_planner.weekly` — fungicide spray-or-wait on a forecast ensemble with CRN,
  risk-neutral default (CVaR-80 option), SOP spray windows (`fa_planner.sop`);
  herbicide none/blanket/spot from the weed map; inoculum-alert input.
* **Scouting flies only when it could change the decision** (planner undecided:
  spraying wins in 25–75 % of members) — routine weekly scouting lost money in
  the audit, and with a "risk this week" trigger alone the twin flew 7 flights
  in an uninfected season.
* **At least 10 forecast members.** With 4, an infected season got 4 sprays
  instead of 8 and lost 6.6 % yield.
* **The planner starts from what has already happened** (`member_blight_state`):
  dated scouting results (negatives included) and recorded sprays are replayed
  through the observed weather; an unseen past infection is inferred from the
  observed favourable spells; an infection a negative flight would have seen is
  rejected. Before this fix a detected epidemic restarted from the first scouted
  severity every week and the twin waited while severity reached 0.75 in one
  season; after it, the same season ended at 0.14.
* **The default objective stays risk-neutral.** An earlier run the same day put
  CVaR-80 ahead (+₹2,379/ha); that run used the planner bug above, and with
  blight state propagated risk-neutral beat CVaR by ₹759/ha (95 % CI 390–1,112),
  with fewer sprays and equal yield. Recorded because the reversal is the kind
  of thing this log exists for.
* Spray advice is refused for crops without a damage model (only potato has one).
* `fa_loop.orchestrator.FieldTwin` runs the same code operationally (API) and
  against a simulated truth (`fa_loop.season`). The twin receives observed weather
  one day at a time (`POST /twin/{field}/weather`) and plans over members composed
  of observed past + a supplied 15-day forecast + climatology to harvest; a
  request for a day it has no weather to reach is refused (422). The season
  simulator feeds the twin the same way. The one camera pass goes through the
  EnKF (16 members, cover + stage) and the irrigation twin is rebuilt with the
  posterior parameters.

### 7. Router: tank capacity and spot spraying
Liquid is a node demand capped by the tank per sortie; spot mode scales liquid
by each swath's treated fraction and skips clean swaths. A 6 ha broadcast job
with a 10 L tank at 20 L/ha now needs ≥ 12 sorties (the audit caught the repo
planning it as 1). The independent verifier checks liquid as well as energy.

## What this does not change

Nothing has flown or been measured in a field. Blight, weed and price figures
are assumptions; no crop parameters are calibrated for India; the weather is a
generator, not station data.
