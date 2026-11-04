# Validation: Mead, Nebraska (AmeriFlux US-Ne1, US-Ne2, US-Ne3), maize 2001-2007

The question: if this software had run on a real, measured farm, how close would it have
been, and where does it do better than a generic crop model?

## The farm

These are three University of Nebraska fields within 1.6 km of each other at Mead. They are
among the most thoroughly measured crop fields in the world:

| Field | Water | Rotation | Maize seasons | Soil (SSURGO) |
|---|---|---|---|---|
| US-Ne1 | centre-pivot irrigated | continuous maize | 2001-2007 (7) | Yutan silty clay loam |
| US-Ne2 | centre-pivot irrigated | maize-soybean | 2001, 03, 05, 07 | Filbert silt loam |
| US-Ne3 | rainfed | maize-soybean | 2001, 03, 05, 07 | Tomek silt loam |

There are 15 maize site-years. All 15 have in-season leaf area index (LAI) from destructive
sampling, and 11 have a measured harvest yield and above-ground biomass. Ground truth comes
from the AmeriFlux BADM files (CC-BY-4.0):

* Yield is grain carbon / 0.45, reported at 15.5 % moisture.
* Biomass is total carbon / 0.439.

None of the measurements below was shown to the model, except the three training seasons named
in "Calibration".

## What the simulation was given (all real)

| Input | Source |
|---|---|
| Field | the pivot circle at each tower |
| Weather | NASA POWER daily 1984-2007, with gauge rain and temperature from NOAA GHCN-Daily USC00255362 (MEAD 6 S, 5.3 km) |
| Soil | USDA NRCS SSURGO via Soil Data Access (map unit at each tower); SoilGrids 2.0 in v1-v3 |
| Canopy seen from space | Landsat 5/7 Collection 2 L2 surface reflectance, clear pixels over each pivot (Microsoft Planetary Computer) |
| Management | sowing date and plant population, 2001-2003: University of Nebraska Carbon Sequestration Program site records; tillage history: AmeriFlux BADM site descriptions |
| Hybrid | ~113 CRM, 2750 GDU to black layer (the site's planting records) |

Two inputs were not documented and were assumed:

* For 2004-2007, sowing date and population are each field's 2001-2003 mean.
* Residue cover is from Eck & Brown 2004, not measured.

## Model versions

| Version | What changed | Why |
|---|---|---|
| v1 | WOFOST 7.2 grain maize as shipped, gridded weather, SoilGrids 0-60 cm, fixed 1 May sowing | a generic crop model |
| v2 | + 0-150 cm root zone, maize roots to 150 cm, hybrid thermal time from the GDU rating, soil water at sowing from the winter's weather | physics the generic set-up leaves out |
| v3 | + one regional hybrid calibrated on 3 irrigated seasons (leaf life, early leaf growth, seed weight, sowing offset) | WOFOST's maize is a European variety |
| v4 | + measured sowing date and stand, residue mulch (FAO-56), rain gauge, SSURGO, root front 3.1 cm/d; hybrid recalibrated on the same 3 seasons | the field as it was actually farmed |

Each version is scored three ways:

* **at sowing**: weather of the season, no satellite;
* **15 July**: the twin anchored to the Landsat cover seen so far;
* **end of season**: all of the season's Landsat.

## Calibration

The calibration set is Ne1 2001, 2002 and 2003 (irrigated). It is used only to fit the
regional hybrid's leaf life (SPAN), early leaf growth (RGRLAI), initial weight at 80 000
plants/ha (TDWI) and emergence thermal time (TSUMEM). The loss combines LAI, biomass and yield.

Every other site-year is held out, including every rainfed season. A single irrigated field
taught the model the hybrid. Nothing taught it the rainfed field.

## Results: yield at 15.5 % moisture, held-out seasons

Eight held-out site-years have a measured yield: five irrigated and three rainfed. Ne1 2006-07,
Ne2 2007 and Ne3 2007 have LAI but no published yield. Errors are model minus measured, in t/ha.

**Forecast at sowing (no satellite)**

| | irrigated RMSE (bias) | rainfed RMSE (bias) | all RMSE |
|---|---|---|---|
| v1 generic WOFOST | 4.66 (−3.40) | 6.79 (−6.78) | 5.56 |
| v2 + root zone, hybrid, soil water | 6.05 (−5.20) | 6.50 (−6.49) | 6.23 |
| v3 + regional hybrid | 2.23 (+0.01) | 4.56 (−4.41) | 3.30 |
| **v4 + real management, gauge, soil survey, residue** | **1.77 (−0.36)** | **0.93 (−0.86)** | **1.51** |
| naive: training-season mean (12.0 t/ha) | 1.55 | 3.53 | 2.48 |

**v4 with satellite**

| | irrigated | rainfed | all |
|---|---|---|---|
| 15 July (Landsat so far) | 2.08 | 1.15 | 1.79 |
| end of season (all Landsat) | 2.36 | 1.29 | 2.03 |

**Per site-year (t/ha)**

| | measured | v1 | v3 | v4 at sowing | v4 15 July |
|---|---|---|---|---|---|
| Ne1 2004 | 10.05 | 12.6 | 14.1 | 12.1 | 13.1 |
| Ne1 2005 | 11.47 | 8.4 | 11.7 | 12.4 | 13.2 |
| Ne2 2001 | 13.62 | 7.3 | 13.1 | 12.2 | 12.7 |
| Ne2 2003 | 14.15 | 8.5 | 11.6 | 11.2 | 11.2 |
| Ne2 2005 | 12.83 | 8.4 | 11.7 | 12.3 | 13.0 |
| Ne3 2001 (rainfed) | 8.81 | 2.6 | 5.8 | 7.5 | 8.0 |
| Ne3 2003 (rainfed, drought) | 7.81 | 0.4 | 2.0 | 7.4 | 6.2 |
| Ne3 2005 (rainfed) | 8.94 | 2.2 | 4.5 | 8.1 | 8.2 |

## Where the gain comes from: ablation

Each row removes one v4 input, keeping the calibrated hybrid fixed. The table shows the
forecast at sowing on the same eight seasons.

| Removed | irrigated RMSE | rainfed RMSE |
|---|---|---|
| nothing (v4) | 1.77 | 0.94 |
| sown plant population (use 80 000/ha) | 1.77 | **3.62** |
| rain gauge (gridded weather only) | **3.18** | **2.99** |
| residue mulch | 1.77 | 1.20 |
| SSURGO (SoilGrids root zone instead) | 1.77 | 1.23 |
| real sowing date (1 May) | 2.22 | 0.98 |
| root front 3.1 cm/d (shipped 2.2) | 1.77 | 0.98 |

The irrigated runs use WOFOST's potential-production mode, so soil, residue and roots cannot
affect them. The hybrid was calibrated with gauge weather, so part of the "no gauge" loss is
calibration mismatch.

## What this shows, and what it does not

* **Better than a generic crop model, by a wide margin.** The error at sowing falls from 5.6
  to 1.5 t/ha. The gain is largest where it matters most: on the rainfed field the generic
  model predicted 0.4-2.6 t/ha in seasons that yielded 7.8-8.9, and v4 is within 0.4-1.3 t/ha.
* **Better than "use the average", but only where yield varies.** On the irrigated pivots,
  whose yields stay within ±1.5 t/ha of 12, the naive mean (1.55) is as good as the model
  (1.77). The model earns its keep on the rainfed field (0.93 against 3.53). There it also
  forecasts the drought year (2003: 7.4 against 7.81 measured) without seeing it in any
  calibration.
* **What drove it was the field as farmed.** A rainfed field sown at about 55 000 plants/ha
  carries a smaller canopy, spends its water more slowly and still has some for grain fill.
  Give the model the irrigated stand and it burns the soil water by flowering. That one
  management fact was worth 2.7 t/ha of error. The local rain gauge was worth about 2 t/ha.
* **The rainfed canopy is now too small.** LAI bias is −1.4 and biomass bias −2.6 t/ha. So
  v4's rainfed yield is right partly because errors compensate. If the stand enters as the
  square root of the population ratio, the canopy and biomass match the measurements (LAI RMSE
  0.99, biomass bias −0.1). Yield is then 1.4 t/ha low. That points to a structural gap:
  WOFOST 7.2 does not move stem reserves into the grain under drought. These are diagnostics
  on test data; the stand formulation was fixed before they ran.
* **The satellite does not yet improve yield.** Anchoring to Landsat cover improves the
  irrigated canopy (LAI RMSE 1.02 → 0.76) but not the yield. Two reasons:
  * Landsat sees a pivot every 16 days at best, and canopy cover saturates above LAI ~4.
  * The anchor fits timing (TSUM1, TSUM2, leaf life, early growth), not canopy size, so it
    cannot correct the rainfed canopy.

  Adding canopy size to the anchor, and using Sentinel-2 (5-day) from 2017, are the next steps.
* **Unexplained:** Ne1 2004 (measured 10.05) is over-predicted by every version. The cause is
  not in the weather or the canopy the model sees. Nitrogen, hail and maize diseases are not
  modelled.

## Decisions on the rainfed pivot, 2017-2023 (simulated)

`ml/mead/decisions.py` runs Ne3 in each maize season with that year's real weather, its
Sentinel-2 cover, and the field's recorded seeding rate, sowing date and hybrid (BADM). Each
season has 2 seeds, and the three policies are no spraying, calendar (blanket herbicide) and
twin (scan, then spot / blanket / none).

| | none | calendar | twin |
|---|---|---|---|
| mean net, USD/ha (4 seasons) | 1907 | 1906 | 1898 |
| mean herbicide, L/season | 0 | 734 | 120 (−84 %) |
| best net in season | 2019, 2021 | 2017, 2023 | none |

The twin used 84 % less herbicide and stayed within $9/ha of the best policy. It did not
earn more, because the default weed pressure costs only ~6.6 % of yield untreated.

Simulated against the combine, the sprayed (calendar) yield was +1 % in 2021 (12.01 vs
11.89 t/ha) and +18 % in 2017 (14.25 vs 12.05).

Mead has no weed or disease records, so this is a decision-value estimate, not a
validated result.

## Gains beyond yield

### Irrigation water, 2014-2021 (real metered passes vs the twin's scheduler)

`ml/mead/water.py` compares the farm's real irrigation with the twin's. It covers every irrigated
maize season on Ne1 and Ne2 from 2014 to 2021, 11 site-years in all. The farm's actual pivot
passes come from the BADM management records:

* Each pass's field-average depth was transcribed by hand.
* A rain-interrupted partial circle counts as depth × degrees covered / 360.
* Fertigation passes are applied on both sides, because they were made to apply nitrogen.

Each season also uses the recorded sowing date, seeding rate (95% assumed emergence), hybrid
GDU and combine yield.

The shipped `IrrigationScheduler` is used unchanged:

* It triggers at 55% of plant-available water.
* Each pass is 1.25 in, the farm's own standard pass.
* Passes are at least 4 days apart, one pivot rotation.
* It stops at full dent.
* It was not tuned on these seasons.

The **blind** variant shows the twin's own errors. It decides with gridded weather only (no rain
gauge) and the global soil map (SoilGrids). Its schedule is then applied to the field as it was:
gauge weather and soil-survey soil.

| per season, 11 site-years | farm | twin | twin, blind |
|---|---|---|---|
| irrigation, mm | 133 | 63 (−53%) | 72 (−46%) |
| pivot passes, total | 61 | 29 | 32 |
| simulated yield, t/ha | 13.68 | 13.66 | 13.58 (−0.7%) |
| pumping diesel saved, L/ha* | | 61 | 53 |
| CO₂ saved, kg/ha* | | 163 | 143 |

\*At 2.35 gal diesel per acre-inch: UNL CropWatch, 125 ft lift and 35 psi, at 80% pump
performance. Each 1 mm saved is 10 m³/ha of groundwater.

Without irrigation the simulated yield is 12.78 t/ha, so the farm's water was worth 0.9 t/ha on
average. In the 2020 drought it was worth 6 t/ha.

The blind twin's worst season was that drought, Ne1 2020. It applied 112 mm against the farm's
260 mm and lost 1.1 t/ha (−7.5%). The no-gauge, global-soil-map twin under-estimated how fast
the soil was drying. That is why the rain gauge and the soil survey are worth having on the
farm. With them, the 2020 loss was 0.13 t/ha on 176 mm.

The model also had to reproduce the farm's yield under the farm's own irrigation. These 9
seasons have a combine yield, and none was used to build the model:

* RMSE 2.11 t/ha, bias +0.59.
* Without Ne1 2018, RMSE 1.38, bias +0.04. That season yielded 8.5 t/ha after a planter set at
  the wrong vacuum, and the model cannot see that.
* Ne2 2021 (16.95 t/ha) is 3.1 t/ha under-predicted. The hybrid was calibrated on 2001-2003
  genetics.

Read the saving with its limits:

* WOFOST 7.2 does not penalise over-watering, and does not model the nitrate leached by extra
  water.
* The yield under the twin's schedule is simulated, not harvested.
* A published survey of Nebraska irrigated maize put the attainable saving at up to 32% with
  better scheduling (Grassini et al. 2011). The 46-53% here is above that, on a research farm in
  mostly wet years.
* A field trial against flow meters and soil-moisture probes is what would confirm it.

### How early the forecast is right (`ml/mead/leadtime.py`)

The forecast was made on 8 dates for the 8 held-out seasons. Each uses the real weather up to
that day, then each of 17 past years' weather for the rest of the season, averaged.

| forecast date | days before harvest | RMSE all | irrigated | rainfed | range across past weather |
|---|---|---|---|---|---|
| sowing | 156 | 1.54 | 1.63 | 1.38 | 4.35 |
| 1 June | 136 | 1.49 | 1.67 | 1.13 | 4.40 |
| 1 July | 106 | 1.55 | 1.79 | 1.05 | 4.07 |
| 1 Aug | 75 | 1.31 | 1.45 | 1.02 | 4.54 |
| 1 Sep | 44 | 1.45 | 1.69 | 0.90 | 2.79 |
| harvest (all weather known) | 0 | 1.51 | 1.77 | 0.94 | 0 |

* **The forecast at sowing is as good as the end-of-season hindcast.** It is available 5 months
  before the combine and about 8 months before USDA's county estimate.
* On the rainfed field the error falls to about 1.0 t/ha by 1 July, more than 3 months ahead.
* The remaining error is the model's, not the unknown weather. The range across past weather
  years tells the farmer how wide the outcomes can still be on that date.

### Speed

Each run is one CPU core of this workspace.

| task | time |
|---|---|
| one full-season simulation | 0.2 s |
| one forecast (17 weather years) | 3.2 s |
| one season's irrigation study (7 policies, daily decisions) | 1.6 s |
| one season of the Ne3 decision study (field build, 3 policies, drone scans) | 30-100 s |

### Herbicide (from the decision study above)

On the rainfed pivot, 2017-2023, the twin used 84% less herbicide than blanket spraying (120 vs
734 L per season) for the same simulated profit. This is simulated; Mead has no weed records.

### Not measured here

* **Nitrogen:** the farm applied 140-230 lb N/acre, but WOFOST 7.2 has no nitrogen cycle.
* **Fungicide:** no disease records.
* **Labour hours:** only the 29 fewer pivot passes, about 100 h of pivot running time each,
  appear above.

## v5: stem reserves, canopy size, Sentinel-2 (tested, not shipped)

Three changes were built and tested together (`ml/mead/v5.py`, [ADR 0021](../adr/0021-remobilisation-canopy-sentinel2.md)):

* **Stem reserves to grain under drought:** CERES-Maize's rule. Stem is drawn down to 85% of
  its silking weight when drought leaves the grain short of demand. No fitted parameter.
* **Canopy size in the satellite anchor:** the ensemble fits the leaf share of new growth as
  well as timing. A satellite-cover operator with k = 0.33 was fitted on Ne1 2001-2003 only.
* **Sentinel-2 from 2017:** 26 usable scenes per season against 4.6 from Landsat.

### The clean test: 12 seasons, 2014-2021, combine yields

None of these seasons was used to build v4 or to choose v5. The irrigated pivots run with
the farm's metered passes. The rainfed pivot runs with its recorded sowing date, seeding rate
and hybrid.

| field | year | water | measured | v4 forward | v4 anchored | v5 15 July | v5 anchored | scenes (v5) |
|---|---|---|---|---|---|---|---|---|
| Ne1 | 2014 | irrigated | 11.1 | 11.9 | 13.5 | 11.9 | 11.9 | 3 Landsat |
| Ne1 | 2015 | irrigated | 11.9 | 13.2 | 14.3 | 13.2 | 13.4 | 4 Landsat |
| Ne1 | 2016 | irrigated | 12.7 | 12.5 | 12.2 | 12.5 | 12.2 | 5 Landsat |
| Ne1 | 2017 | irrigated | 14.1 | 15.5 | 16.8 | 15.8 | 15.1 | 13 S2 |
| Ne1 | 2018 | irrigated | 8.5 | 13.5 | 13.7 | 12.2 | 13.8 | 33 S2 |
| Ne1 | 2020 | irrigated | 13.7 | 14.6 | 12.9 | 12.6 | 12.6 | 34 S2 |
| Ne1 | 2021 | irrigated | 14.2 | 13.8 | 15.1 | 12.4 | 11.4 | 30 S2 |
| Ne2 | 2015 | irrigated | 13.5 | 13.2 | 14.1 | 13.2 | 13.3 | 4 Landsat |
| Ne2 | 2021 | irrigated | 16.9 | 13.8 | 15.3 | 12.9 | 13.2 | 29 S2 |
| Ne3 | 2015 | rainfed | 11.7 | 11.8 | 11.9 | 13.8 | 14.0 | 5 Landsat |
| Ne3 | 2017 | rainfed | 12.1 | 14.9 | 14.3 | 15.1 | 14.8 | 11 S2 |
| Ne3 | 2021 | rainfed | 11.9 | 12.9 | 11.9 | 13.2 | 12.2 | 32 S2 |

Yield RMSE, t/ha:

| | forward (at sowing) | 15 July | anchored (full season) |
|---|---|---|---|
| v4 | 2.03 | 2.24 | 2.16 |
| v5 | 2.03 | 2.17 | 2.39 |
| v4, without Ne1 2018 | 1.49 | 1.58 | 1.61 |
| v5, without Ne1 2018 | 1.49 | 1.96 | 1.91 |
| mean of these 12 seasons (a bound no forecast has) | 1.98 | | |

What it shows:

* **Stem reserves:** the rule moved nothing in all 12 seasons, so v5 forward equals v4
  forward. The irrigated pivots never ran short of water, and 2015, 2017 and 2021 were not
  drought years on the rainfed pivot. The only season where it fired was Ne3 2003. There it
  took the forecast from 7.4 to 8.6 t/ha against a measured 7.8, so the miss went from -0.4
  to +0.7 t/ha.
* **Canopy size:** worse on the clean test. On the irrigated fields the bias went from
  +1.2 to 0.0, but the scatter grew. On the rainfed field the anchored RMSE went from 1.30 to
  2.09: Ne3 2015 was pushed from 11.9 to 14.0 against a measured 11.7.
* **Sentinel-2:** six times the scenes, no gain. On the 7 seasons with both sensors the
  anchored RMSE was 2.93 with Sentinel-2 and 2.64 with Landsat. More scenes let the canopy-size
  fit move further, and it moved the wrong way on Ne1 2021 and Ne2 2021.
* **Ne1 2018** came in at 8.5 t/ha. Every version simulated about 13.5, and its 33 Sentinel-2
  scenes did not pull the forecast down. The farm records give no cause the model
  could use. Leaving this one season out lowers every RMSE above by a fifth to a quarter.
* The model forecast at sowing (1.49 without 2018) is about as good as the seasons' own
  mean (1.59), and no better. The 2001-2007 held-out result (1.51 against a naive 2.48) came
  from a wider spread of seasons, rainfed droughts among them. On the 2014-2021 seasons,
  which were mostly good years, the model has little spread to explain.

### On 2001-2007 (diagnostic only)

On the 8 held-out site-years from v1-v4, v5 looked better:

| | forward | 15 July | anchored |
|---|---|---|---|
| v4 | 1.51 | 1.79 | 2.03 |
| v5 | 1.52 | 1.36 | 1.57 |
| v5, rainfed (3) | 0.97 | 0.54 | 0.39 |

On the rainfed pivot, anchored biomass came much closer to measured. For Ne3 2005 it was
16.2 t/ha against a measured 16.8 (v4 gave 12.3). But these seasons were open during
development: three of them were used for quick checks while the anchor was built. The
2014-2021 result is the one to trust, and it says no.

**Decision:** the software keeps the v4 defaults (remobilisation off, timing-only anchor,
either sensor). All three features remain available as options:

* `ZoneConfig(remobilisation=True)`;
* `anchor_to_cover(..., **fa_farm.anchor.SATELLITE)`;
* `fa_farm.sources.satellite.s2_cover_series`.

## Reproduce

```
python ml/mead/study.py fetch-historic      # weather, Landsat, soil  -> ml/mead/data/
python ml/mead/study.py calibrate-v4        # Ne1 2001-2003 only      -> ml/results/mead/hybrid_calibration_v4.json
python ml/mead/study.py run-v4              # all 15 site-years       -> ml/results/mead/runs_v4.json
python ml/mead/report.py                    # metrics + figures
python ml/mead/ablation.py                  # which input carries what
python ml/mead/decisions.py --years 2017-2023 --seeds 2   # spraying policies on Ne3
python ml/mead/water.py && python ml/mead/water.py --plot    # irrigation 2014-2021 vs the farm's passes
python ml/mead/leadtime.py && python ml/mead/leadtime.py --plot   # forecast error by date
python ml/mead/v5.py fetch-sat && python ml/mead/v5.py run-fresh   # v5 on the 12 fresh seasons 2014-2021
python ml/mead/v5.py run-2001                                      # v5 on the 2001-2007 site-years
```

## Sources

* AmeriFlux BADM, US-Ne1 / US-Ne2 / US-Ne3, Suyker, A. (University of Nebraska), CC-BY-4.0.
  DOIs: 10.17190/AMF/1246084, 10.17190/AMF/1246085, 10.17190/AMF/1246086.
* University of Nebraska Carbon Sequestration Program, site management records 2001-2003 (csp.unl.edu).
* NOAA NCEI GHCN-Daily, station USC00255362 MEAD 6 S (Menne et al. 2012).
* USDA NRCS SSURGO via Soil Data Access.
* NASA POWER daily meteorology.
* Landsat Collection 2 L2 via Microsoft Planetary Computer.
* Suyker & Verma 2009, Agric. For. Meteorol. 149:443-452 (Mead ET and water-use efficiency).
* Ordóñez et al. 2018, Field Crops Res. 215:122-131 (maize root front velocity).
* Irmak 2014, UNL NebGuide G2245 (corn soil-water extraction to 1.5 m in silt loam).
* Allen et al. 1998, FAO-56 ch. 10 (mulch and soil evaporation).
* Eck & Brown 2004, Purdue AY-269-W (residue cover).
* WOFOST 7.2 via PCSE (de Wit et al. 2019).
* Grassini et al. 2011, Field Crops Res. 120:133-141 (irrigation management and water productivity, Nebraska maize).
* UNL CropWatch, "Conversion for diesel fuel for center pivots" (diesel per acre-inch).
