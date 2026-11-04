# 0021: Stem reserves, canopy-size anchor, Sentinel-2: built, tested, kept off

**Status:** accepted · **Date:** 2026-11-04 · **Evidence:** [Mead validation, v5](../validation/mead.md#v5-stem-reserves-canopy-size-sentinel-2-tested-not-shipped)

## Context

After v4, three gaps looked like the next gains:

* WOFOST 7.2 fills grain only from the day's assimilate. Under drought, maize also draws on
  sugars stored in the stem.
* The satellite anchor fitted canopy timing but not canopy size.
* Landsat gives about 5 usable scenes per season. Sentinel-2 has a 5-day revisit from 2017.

## Decision

All three were built and tested:

* `fa_twin.remobilisation`: the CERES-Maize rule from `MZ_GROSUB.for`, also adopted by
  Hybrid-Maize. The stem gives grain the shortfall below its demand, down to 85% of its
  silking weight. Grain demand is WOFOST's own potential grain growth × (0.45 + 0.55·RFTRA).
  The transfer goes through WOFOST 7.2's REALLOC_ST and REALLOC_SO rates, so mass is conserved.
  No parameter is fitted.
* `fa_twin.assimilation`:
  * a LEAFSHARE factor added to the ensemble's parameters;
  * an FVC observation operator with k = 0.33, fitted on the training seasons only;
  * parameter-only updates.
* `fa_farm.anchor.SATELLITE` bundles these anchor options.
* `ml/mead/v5.py` adds Sentinel-2 series per pivot for 2017-2021.

**None of the three is on by default.** On the 12 fresh Mead seasons (2014-2021, combine
yields, not used before):

* Remobilisation moved nothing: no season was droughty enough.
* The canopy-size anchor raised the full-season RMSE from 2.16 to 2.39 t/ha.
* Sentinel-2 did not beat Landsat (2.93 against 2.64 on the 7 shared seasons).

On 2001-2007 v5 looked better (anchored 2.03 → 1.57), but those seasons had been looked at
during development.

## Consequences

* The defaults are unchanged from v4:
  * `REMOBILISING_CROPS` is empty;
  * `fa_farm.build` uses the timing-only anchor.
* Each feature is one argument away: `ZoneConfig(remobilisation=True)` and
  `anchor_to_cover(..., **SATELLITE)`.
* A fair test of remobilisation needs drought seasons with measured yield. Mead 2014-2021 has
  none. The next rainfed drought season, or another site, would settle it.
* Revisit the canopy-size anchor only with a test set that includes both droughts and good
  years. On 2001-2007 it helped the rainfed droughts; on 2014-2021 it hurt the good years.
