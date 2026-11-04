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
