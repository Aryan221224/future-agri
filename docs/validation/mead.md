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

## Reproduce

```
python ml/mead/study.py fetch-historic      # weather, Landsat, soil  -> ml/mead/data/
python ml/mead/study.py calibrate-v4        # Ne1 2001-2003 only      -> ml/results/mead/hybrid_calibration_v4.json
python ml/mead/study.py run-v4              # all 15 site-years       -> ml/results/mead/runs_v4.json
python ml/mead/report.py                    # metrics + figures
python ml/mead/ablation.py                  # which input carries what
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
