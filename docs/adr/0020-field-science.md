# 0020: Field science, validated against a measured farm

**Status:** accepted · **Date:** 2026-11-04 · **Evidence:** [Mead validation](../validation/mead.md)

## Context

WOFOST simulated rainfed maize at Mead, Nebraska (AmeriFlux US-Ne3) at 2-6 t/ha in seasons
when the field measured 7.8-8.9 t/ha. The cause was not a missing crop process. The model was
growing the wrong field. It had:

* the irrigated fields' stand, while the rainfed field is sown at about 55 000 plants/ha
  against 80 000 irrigated;
* a 100 cm root zone in a 2 m loess;
* soil water guessed at sowing, not derived;
* gridded weather that runs about 1 °C warm and spreads storms into drizzle;
* bare soil under a no-till residue mulch.

The model's canopy reached LAI 6.5 where the field had 4.3. It spent the soil water by
flowering and had none left for grain. Measured biomass shows the real crop still adding
7.8 t/ha after silking.

## Decision

`fa_farm.science.derive()` runs between the data layers and the crop model, in both the
virtual-farm and the live-field builders. Everything it uses is knowable before sowing, so
the **twin gets it too**: `FarmSpec.science_inputs()`. The truth model additionally gets the
satellite anchor (`FarmSpec.param_overrides`), which the twin never sees.

| Layer | Source | Physics |
|---|---|---|
| Weather | nearest GHCN-Daily gauge ≤ 15 km (`sources/stations.py`) | gauge TMAX/TMIN/RAIN replace gridded values; physically impossible days are rejected |
| Soil | USDA SSURGO in the US (`sources/ssurgo.py`), else SoilGrids 0-150 cm | survey AWC, depth-weighted over the root zone |
| Roots | Ordóñez et al. 2018; Irmak 2014 | RDMCR 150 cm in deep soils; maize root front 3.1 cm/d |
| Hybrid | the seed bag's GDU / CRM rating (`fa_twin.hybrids`) | converted to WOFOST thermal time on the site's own past weather |
| Soil water at sowing | weather since 1 Oct (`fa_twin.soilwater`) | Ritchie two-stage evaporation, snow, drainage |
| Stand | sown population (`fa_twin.management`) | TDWI and leaf allocation scale with plant number; the rest of the allocation goes to stems |
| Residue | tillage and previous crop | FAO-56: 5 % less soil evaporation per 10 % cover (Eck & Brown 2004 cover) |

`ZoneConfig.soil_evap_factor` carries the residue factor into every weather provider: truth,
twin, planner and anchor. The calibrated hybrid ships as regional variety
`("maize", "US_CornBelt_2750GDU")`, with its provenance recorded.

## Consequences

* Every layer degrades on its own. With no gauge, no survey or no history, the previous
  layer stands and a warning names the gap. Offline the builders still work.
* Farms pickled before this change still load: missing fields read as "no science".
* Results change for any farm built with `science=True` (the default). Older studies can be
  reproduced with `science=False`.
* What stays unmodelled is listed in the validation report: nitrogen, hail and disease in
  maize, and the stand's effect on per-plant ear size.
