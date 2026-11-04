"""Field science: what can be known about a field before the season, beyond the defaults.

Validated against a measured farm (Mead, Nebraska, AmeriFlux US-Ne1/2/3, 2001-2007; see
docs/validation/mead.md). Every step degrades gracefully: if a source is unreachable the
previous layer stands and a warning says so.

  weather   nearest GHCN-Daily rain gauge / thermometer within 15 km replaces gridded
            TMAX/TMIN/RAIN (gridded products run ~1 C warm and smear storms into drizzle,
            which over-states soil evaporation) -- fa_farm.sources.stations
  soil      USDA SSURGO inside the US (the mapped soil series' horizons), else SoilGrids
            0-150 cm root zone; the 0-60 cm topsoil prior stays as the fallback
  rooting   maize/wheat/sorghum/sunflower/cotton roots to 150 cm in deep soils, maize root
            front at 3.1 cm/day (Ordonez et al. 2018) -- fa_twin.hybrids
  hybrid    a maize hybrid's GDU-to-black-layer rating converted to WOFOST thermal time on
            the site's own historical weather -- fa_twin.hybrids
  water     soil water at sowing from the weather since 1 October (Ritchie two-stage
            evaporation, snow, drainage) -- fa_twin.soilwater
  stand     sown plant population -> TDWI and leaf allocation -- fa_twin.management
  residue   tillage and previous crop -> surface cover -> FAO-56 soil evaporation factor
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from fa_twin.engine import SoilParams

from .sources.base import Cache

RRI_MAIZE = 3.1                       # cm/day after V5 (Ordonez et al. 2018, Field Crops Res. 215:122)


@dataclass
class Management:
    """What the farmer knows at sowing. Everything optional."""
    population: float | None = None          # plants/ha
    tillage: str | None = None               # "no-till" | "reduced" | "conventional"
    previous_crop: str | None = None
    gdu_black_layer: float | None = None     # maize hybrid rating, deg F GDU (86/50)
    crm: float | None = None                 # or its relative maturity, days

    @classmethod
    def from_dict(cls, d: dict | None) -> "Management":
        d = d or {}
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if d.get(k) is not None})


@dataclass
class FieldScience:
    soil: SoilParams
    WAV: float | None
    overrides: dict
    soil_evap_factor: float
    weather: list[dict]
    provenance: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"soil": self.soil.as_dict(), "WAV": self.WAV, "overrides": self.overrides,
                "soil_evap_factor": self.soil_evap_factor, "notes": self.notes, "warnings": self.warnings}


def derive(lat: float, lon: float, crop: str, variety: str, sowing: dt.date, records: list[dict],
           cache: Cache, soil_prior: SoilParams, elevation: float = 200.0,
           management: Management | dict | None = None, use_gauges: bool = True,
           use_soil_survey: bool = True) -> FieldScience:
    """Field-specific inputs for the crop model. ``records``: daily weather (history + season),
    sorted; the merged copy comes back in ``.weather``."""
    from fa_twin.hybrids import crop_table, gdu_for_crm, rooting_override, tsums_for_hybrid
    from fa_twin.management import population_overrides, residue_cover, soil_evaporation_factor
    from fa_twin.soilwater import available_water_at_sowing
    mg = management if isinstance(management, Management) else Management.from_dict(management)
    prov, notes, warns = [], [], []
    wx = list(records)

    if use_gauges and wx:
        from .sources.stations import merge_station
        wx, p, info = merge_station(wx, lat, lon, wx[0]["DAY"], wx[-1]["DAY"], cache)
        if p is not None:
            prov.append(p)
            notes.append(f"weather: gauge {info['station']['name']} ({info['station']['km']} km) for rain and temperature")
        else:
            warns.append(f"no rain gauge used ({info.get('note')}); weather is gridded only")

    soil = soil_prior
    if use_soil_survey:
        try:
            from .sources.ssurgo import ssurgo_rootzone
            _, soil, p = ssurgo_rootzone(lat, lon, cache)
            prov.append(p)
            notes.append("soil: USDA SSURGO soil survey, 0-150 cm")
        except Exception:  # noqa: BLE001 -- outside the US, or SDA down
            try:
                from .sources.soil import soilgrids_rootzone
                _, soil, p = soilgrids_rootzone(lat, lon, cache)
                prov.append(p)
                notes.append("soil: SoilGrids 2.0, 0-150 cm root zone")
            except Exception as e:  # noqa: BLE001
                warns.append(f"root-zone soil unavailable ({e}); topsoil prior kept")

    overrides: dict = {}
    try:
        cp = crop_table(crop, variety)
    except Exception:  # noqa: BLE001 -- crop not in the WOFOST library
        cp = None
    if cp is not None:
        overrides.update(rooting_override(crop, soil.RDMSOL, float(cp.get("RDMCR", 100.0))))
        if crop == "maize":
            overrides["RRI"] = RRI_MAIZE
            gdu = mg.gdu_black_layer or (gdu_for_crm(mg.crm) if mg.crm else None)
            if gdu:
                hist = [r for r in wx if r["DAY"] < sowing - dt.timedelta(days=60)]
                try:
                    ph = tsums_for_hybrid(hist, sowing.strftime("%m-%d"), gdu, cp)
                    overrides.update({"TSUM1": round(ph.TSUM1, 1), "TSUM2": round(ph.TSUM2, 1)})
                    notes.append(f"phenology: hybrid rated {gdu:.0f} GDU, converted on {ph.seasons} past seasons")
                except ValueError as e:
                    warns.append(f"hybrid rating not converted ({e})")
        if mg.population:
            pop = population_overrides(crop, {**cp, **overrides}, mg.population)
            if pop:
                overrides.update(pop)
                notes.append(f"stand: {mg.population:,.0f} plants/ha")

    cover = residue_cover(mg.tillage, mg.previous_crop)
    sef = soil_evaporation_factor(cover)
    if cover:
        notes.append(f"residue: {mg.tillage} after {mg.previous_crop or 'fallow'}, ~{cover:.0%} cover, "
                     f"soil evaporation x{sef:.2f}")

    wav = None
    try:
        w = available_water_at_sowing(wx, soil.SMFCF, soil.SMW, sowing, soil.RDMSOL, lat=lat, elev=elevation,
                                      evap_factor=sef)
        wav = w["WAV_cm"]
        notes.append(f"soil water at sowing: {wav:.1f} of {w['capacity_cm']:.1f} cm from the weather since 1 October")
    except ValueError as e:
        warns.append(f"soil water at sowing not estimated ({e}); default used")
    return FieldScience(soil, wav, overrides, sef, wx, prov, notes, warns)
