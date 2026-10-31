# 0005 — PCSE/WOFOST as the twin engine

**Status:** accepted · **Date:** 2026-09-09

## Decision

PCSE 6.0.13 (EUPL-1.2), engine `Wofost81_NWLP_CWB_CNB` where nitrogen response
is needed and `Wofost72_WLP_CWB` as the better-benchmarked fallback. Crop
parameters vendored from `ajwdewit/WOFOST_crop_parameters` at pinned SHAs.

## Alternatives rejected

| candidate | verdict |
|---|---|
| AquaCrop-OSPy (Apache-2.0) | usable second opinion, but no nitrogen response — caps it for advisory use |
| DSSAT via `DSSATTools` | GPL-3.0, and shells out to a separately-installed Fortran binary |
| APSIM via `apsimNGpy` | wrapper is Apache-2.0 but APSIM itself is non-commercial-unless-licensed; .NET runtime dependency |
| SIMPLE crop model | ~100 lines to reimplement, but then we own 100 % of calibration and get no real water/N balance |
| `cropbox` | **name trap** — `pip install cropbox` installs an unrelated media-cropping tool. The real Cropbox is Julia-only. |

## Operational decisions taken because of what broke

Four things failed on first contact and each produced a permanent guard:

1. **Parameter branches are not interchangeable.** The `wofost81` branch drops
   `AMAXTB` and adds N tables; loading it into a 7.2 engine fails with
   `Value for parameter AMAXTB missing`. Both branches are vendored and selected
   by engine (`PARAM_BRANCH`).
2. **Branch coverage is uneven.** On `wofost81`, rice ships 9 varieties but only
   **1** carries `AMAX_REF`; maize ships 7 and 1 is complete. Selecting any
   other raises at construction. `variety_support()` enumerates what an engine
   can actually run, and the UI must call it before offering a cultivar list.
3. **Rice needs `crop_start_type="emergence"`.** Its parameter sets carry
   `TSUMEM=0` because the crop is transplanted, and a "sowing" start divides by
   zero. `resolve_start_type()` detects this from the parameters.
4. **`apply_npk` does not exist in PCSE 6.x** — the signal is `apply_n`, and P
   and K are not simulated in this configuration. A prescription naming P or K
   is reported as N-only rather than silently pretending it had an effect.

## Vendoring, not fetching

`YAMLCropDataProvider` downloads parameters from raw.githubusercontent.com on
first use. That makes an air-gapped field gateway fail, and makes results depend
on whatever was on a branch that day. Both branches are vendored at pinned SHAs
(`vendor/WOFOST*_SHA.txt`).

## Licensing watch-item

PCSE is **EUPL-1.2** — weak copyleft with a distribution/network trigger, not
MIT/Apache. The crop-parameter repository has **no LICENSE file at all**; each
YAML declares `Rights: European Union Public License (EUPL)` in its metadata,
and that in-file field is the only grant. An unlicensed repository defaults to
all-rights-reserved. Get written confirmation before shipping commercially.

## The honest part: calibration

None of the shipped parameters are calibrated for India.

* Wheat: 14 European cultivars, all `IDSL=2` — see ADR 0011, they cannot
  complete development at Indian latitudes at all.
* Rice: 3–9 cultivars, the relevant ones being IR8 (a 1960s HYV) and IRRI
  hybrids. No Indian releases, nothing for basmati.
* **Cotton and sugarcane: one generic set each, Van Heemst 1988, `Region: "No
  specific region"`.** These are placeholders. The twin marks them
  `uncalibrated_parameters: true` and attaches a warning that propagates to the
  UI, because a placeholder that looks like a calibration is the most expensive
  bug available in this subsystem.
* **Structural gap:** WOFOST has no puddled-transplanted-rice water balance, and
  Indian kharif rice is grown in exactly that system. That is not calibratable
  away.

Realistic budget: **1–2 seasons of field data per crop × agro-ecological zone**
before absolute yield is decision-grade. Phenology (TSUM1/TSUM2) first — it
fixes most of the error and needs only sowing/anthesis/maturity dates.

**Product consequence:** lead with *relative* signals ("this field is 15 %
behind the district norm"), which are defensible far earlier than absolute
kg/ha. That is the difference between shipping in one season and three.
