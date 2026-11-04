"""Field management the crop model has no parameter for: plant population and crop residue.

**Plant population.** WOFOST works per square metre and has no plant density. In maize the
number of leaves per plant is fixed by the hybrid, so below the optimum population the canopy's
leaf area scales with the number of plants. Mead's rainfed field (53 000-58 000 plants/ha)
carried ~70 % of the irrigated fields' leaf area (77 000-82 000/ha) from the first weeks, before
any water stress (AmeriFlux US-Ne1/Ne3 LAI, 2001-2007; populations: UNL Carbon Sequestration
Program site records). The variety's parameters are taken to describe its calibration
population; another population scales the initial crop weight (TDWI, seed number) and the
share of new growth going to leaf blades (FLTB), the rest going to stems (FSTB), so the leaf
area follows the plant count and the light the canopy does not take still reaches the soil.
Bounded to 0.4-1.3x the reference: outside that range per-plant plasticity dominates.

**Residue.** Crop residue left on the surface cuts soil evaporation by ~5 % for every 10 % of
the surface it covers (Allen et al. 1998, FAO-56, ch. 10 "organic mulches"). Cover after
no-till planting follows Eck & Brown (2004, Purdue AY-269-W): ~90 % (corn) / ~80 % (soybean)
after harvest x 75-85 % surviving winter x 85-95 % surviving a no-till planter, i.e. ~70 %
after maize or small grains and ~50 % after (fragile) soybean residue; ~10 % after a
full-width disk or plough. Over winter residue explains 71 % of the variation in
non-growing-season evaporation at Mead (Suyker & Verma 2009, Agric. For. Meteorol. 149:443).
"""
from __future__ import annotations

REFERENCE_POPULATION = {"maize": 80000.0}           # plants/ha a variety's parameters describe
POP_BOUNDS = (0.4, 1.3)

RESIDUE_COVER = {                                   # surface cover fraction after planting
    ("no-till", "soybean"): 0.50, ("no-till", "maize"): 0.70, ("no-till", "wheat"): 0.70,
    ("no-till", "sorghum"): 0.65, ("no-till", "cotton"): 0.25, ("no-till", "fallow"): 0.20,
    ("reduced", "soybean"): 0.25, ("reduced", "maize"): 0.40, ("reduced", "wheat"): 0.40,
    ("conventional", "*"): 0.10,
}


LEAF_SHARE_BOUNDS = (0.4, 1.6)


def leaf_share_overrides(params: dict, r: float) -> dict:
    """FLTB x ``r`` with stems taking exactly the complement (FSTB), so leaves + stems + storage
    organs still sum to 1. ``r`` is bounded to 0.4-1.6 and a leaf share never takes more than
    the stems had. Used for plant population and for the satellite canopy-size fit."""
    r = min(max(float(r), LEAF_SHARE_BOUNDS[0]), LEAF_SHARE_BOUNDS[1])
    fl, fs = list(params["FLTB"]), list(params["FSTB"])
    if fl[0::2] != fs[0::2]:
        raise ValueError("FLTB and FSTB must share their development-stage points")
    new_fl, new_fs = [], []
    for i in range(0, len(fl), 2):
        leaf = round(min(fl[i + 1] * r, fl[i + 1] + fs[i + 1]), 6)
        # stems take exactly what the leaves gave up: PCSE checks FL+FS+FO == 1 to 1e-4
        new_fl += [fl[i], leaf]
        new_fs += [fs[i], fs[i + 1] + fl[i + 1] - leaf]
    return {"FLTB": new_fl, "FSTB": new_fs}


def population_overrides(crop: str, params: dict, population: float | None,
                         reference: float | None = None) -> dict:
    """WOFOST overrides for a sown population (plants/ha). ``params`` holds the variety's current
    TDWI, FLTB and FSTB (after any other overrides)."""
    ref = reference or REFERENCE_POPULATION.get(crop)
    if not population or not ref:
        return {}
    r = min(max(population / ref, POP_BOUNDS[0]), POP_BOUNDS[1])
    if abs(r - 1.0) < 1e-3:
        return {}
    return {"TDWI": round(float(params["TDWI"]) * r, 3), **leaf_share_overrides(params, r)}


def residue_cover(tillage: str | None, previous_crop: str | None) -> float:
    if not tillage:
        return 0.0
    t = tillage.lower()
    if t in ("conventional", "plough", "plow", "disk"):
        return RESIDUE_COVER[("conventional", "*")]
    return RESIDUE_COVER.get((t, (previous_crop or "fallow").lower()), 0.0)


def soil_evaporation_factor(cover: float) -> float:
    """FAO-56: evaporation reduced ~5 % per 10 % of surface covered by organic mulch."""
    return round(1.0 - 0.5 * min(max(cover, 0.0), 1.0), 3)
