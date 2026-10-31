# 0015 — A global virtual test farm built from real data, with real archived forecasts

**Status:** accepted · **Date:** 2026-10-31 · supersedes the India-only defaults of [0013](0013-post-audit-rebuild.md)

## Context

Future Agri is a global product. Until now every benchmark ran at one site
(Bennett, India) on a normals-calibrated *stochastic* weather generator, with a
potato field whose soil, boundary and season were invented. A twin tested only
against its own generator measures the generator. The research brief (31 Oct
2026) also showed that spray-drone use is global and contractor-led, led by Asia,
with the 10 L class at the small end: the test farms must look like the farms
these drones actually serve.

## Decision

1. **Archetype farms from the research** (`fa_farm/data/archetypes.yaml`): eight
   archetypes from East-Asian paddy to Brazilian soy, US corn, European potato,
   Sub-Saharan smallholder maize, tree crops and Xinjiang cotton, each with real
   example locations, field-size priors, crop calendar and sources. Tree crops and
   cotton defoliation load but **refuse to run** (no crop model can represent them).
2. **Every layer from real open data**, with provenance and licence on each
   number (`fa_farm/sources`):
   field boundary — Fields of The World global predictions (CC-BY-4.0);
   soil — SoilGrids 2.0 → Saxton & Rawls (2006);
   terrain — Copernicus GLO-30 DEM → topographic wetness index;
   weather — NASA POWER daily (open, commercial use allowed; Open-Meteo is
   flagged non-commercial and not the default);
   satellite — Sentinel-2 L2A field-median NDVI → cover, SCL-masked.
3. **The truth is anchored to the real field.** Its crop parameters are fitted by
   EnKF to the field's own Sentinel-2 cover series for that season (post-
   senescence regrowth excluded). Within-field structure (soil zones from a random
   field + TWI, patchy emergence, Neyman–Scott weed clusters, blight foci) is a
   labelled stochastic layer, re-drawn per seed.
4. **Real forecasts drive the future iterations.** The planner's members are the
   ensemble forecasts actually issued on the decision day — ECMWF IFS ENS (51
   members, archive from 2024-04-01), ECMWF AIFS ENS (from 2025-07-02), NOAA GEFS
   (31 members, from 2020-10-01), via dynamical.org (CC-BY-4.0) — bias-corrected on
   the fly against observed weather (rolling pairs, not climatology matching, which
   would erase the forecast anomaly), and completed to harvest with **real analog
   years** from the site's record. The Delhi generator is no longer on the global
   path.
5. **Truth and observation are separated by type.** The twin receives only a
   `DigitalFarm` (scan-derived zones, weed map with uncertainty, blight sightings),
   forecasts and records of what was sprayed; a test asserts no `GroundTruthFarm`
   is reachable from it.
6. **Region packs** (`fa_farm/data/regions/*.yaml`) carry currency, prices, costs
   and regulation constraint packs as data. US (Part 107/137, 55 lb), EU (default
   aerial ban, derogation required) and BR (Portaria 298: 20 m buffers, weather
   records) are marked verified; JP, CN, KR, AU, CA, KE, TH, VN and GLOBAL are
   unverified and every plan under them carries a warning. Prices are illustrative.

## Consequences

* A virtual farm can be built for any location FTW covers; a run is reproducible
  offline once its inputs are cached (content-addressed cache).
* The crop of the sampled field in that season is **not verified** (no open global
  crop-type map was reachable); its cover is used for timing only, and the report
  says so.
* WOFOST still limits fidelity: no paddy water layer (rice runs as potential
  production, flagged), generic Van Heemst parameters for tropical soy and maize.
* Only potato late blight is a modelled disease; other archetype diseases are
  listed as not modelled rather than given invented parameters.
