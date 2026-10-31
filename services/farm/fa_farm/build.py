"""Build a virtual test farm from real data for any archetype, site and season.

    vf = build_virtual_farm("A5", 2025, Cache())
    truth = vf.truth()          # GroundTruthFarm, the 'real farm'

Layers: FTW field boundary (or a user polygon), SoilGrids soil, Copernicus DEM
wetness index, NASA POWER weather (season + 20 years of history for analog
climatology), Sentinel-2 cover of that very field, and anchoring of the crop
parameters to that cover. Every layer's provenance is kept for the report.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np

from fa_twin.engine import ZoneConfig

from .anchor import anchor_to_cover
from .archetypes import load_archetypes, season_dates
from .grid import FarmGrid
from .sources.base import Cache, DataUnavailable
from .sources.boundaries import field_near
from .sources.satellite import s2_cover_series
from .sources.soil import soilgrids_profile
from .sources.terrain import dem_window, twi
from .sources.weather import power_daily
from .truth import FarmSpec, GroundTruthFarm


@dataclass
class VirtualFarm:
    spec: FarmSpec
    season_weather: list[dict]
    history: list[dict]
    s2_series: list[tuple]
    anchor_info: dict
    provenance: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def truth(self) -> GroundTruthFarm:
        return GroundTruthFarm(self.spec, self.season_weather)

    def summary(self) -> dict:
        s = self.spec
        lat, lon = s.centroid
        g = FarmGrid.from_wgs84(s.polygon_wgs84, s.cell_m)
        return {"archetype": s.archetype.id, "name": s.archetype.name, "site": s.site.name,
                "year": s.year, "lat": round(lat, 5), "lon": round(lon, 5),
                "area_ha": round(g.area_ha, 3), "cells": g.n_cells, "cell_m": s.cell_m,
                "crop": s.archetype.crop, "variety": s.archetype.variety,
                "soil": s.soil.as_dict(), "param_overrides": s.param_overrides,
                "s2_observations": len(self.s2_series), "anchor": self.anchor_info,
                "provenance": [p.as_dict() for p in self.provenance], "warnings": self.warnings}


def sample_dem_cells(dem: np.ndarray, lat0: float, lon0: float, half_size_m: float,
                     cells_lonlat: np.ndarray) -> np.ndarray:
    dlat = half_size_m / 111_320.0
    dlon = half_size_m / (111_320.0 * max(0.1, math.cos(math.radians(lat0))))
    ny, nx = dem.shape
    rows = ((lat0 + dlat - cells_lonlat[:, 1]) / (2 * dlat) * ny).astype(int).clip(0, ny - 1)
    cols = ((cells_lonlat[:, 0] - (lon0 - dlon)) / (2 * dlon) * nx).astype(int).clip(0, nx - 1)
    return dem[rows, cols]


def build_virtual_farm(archetype_id: str, year: int, cache: Cache | None = None,
                       site_index: int = 0, cell_m: float = 5.0, seed: int = 0,
                       polygon_wgs84=None, anchor: bool = True, history_years: int = 20,
                       weed_mean_m2: float = 8.0, blight_inoculum: bool | None = None,
                       n_zones: int = 4) -> VirtualFarm:
    cache = cache or Cache()
    a = load_archetypes()[archetype_id]
    if not a.supported:
        raise ValueError(f"archetype {a.id} ({a.name}) not supported: {a.unsupported_reason}")
    site = a.locations[site_index]
    sow, harvest = season_dates(a, year)
    prov, warns = [], []
    if a.fidelity_note:
        warns.append(a.fidelity_note)

    if polygon_wgs84 is None:
        polygon_wgs84, attrs, p = field_near(site.lat, site.lon, site.country, cache, *a.field_ha)
        prov.append(p)
    c = polygon_wgs84.centroid
    lat, lon = float(c.y), float(c.x)

    profile, soil, p = soilgrids_profile(lat, lon, cache)
    prov.append(p)

    grid = FarmGrid.from_wgs84(polygon_wgs84, cell_m)
    minx, miny, maxx, maxy = grid.bounds_local
    half = max(maxx - minx, maxy - miny) / 2 + 150.0
    twi_cells = None
    try:
        dem, res_m, p = dem_window(lat, lon, half, cache)
        prov.append(p)
        tw = twi(dem, res_m)
        twi_cells = sample_dem_cells(tw, lat, lon, half, grid.cells_lonlat())
    except DataUnavailable as e:
        warns.append(f"terrain unavailable ({e}); zones use soil variation only")

    start = dt.date(sow.year - history_years, 1, 1)
    end = harvest + dt.timedelta(days=3)
    recs, p, elev = power_daily(lat, lon, start, end, cache)
    prov.append(p)
    season = [r for r in recs if sow - dt.timedelta(days=60) <= r["DAY"] <= end]
    history = [r for r in recs if r["DAY"] < sow - dt.timedelta(days=60)]
    if not season or season[-1]["DAY"] < harvest:
        raise DataUnavailable("weather history", f"NASA POWER does not yet cover {harvest}")

    overrides, info, series = {}, {"note": "anchoring disabled"}, []
    try:
        series, p = s2_cover_series(polygon_wgs84, sow - dt.timedelta(days=20),
                                    harvest + dt.timedelta(days=10), cache)
        prov.append(p)
    except DataUnavailable as e:
        warns.append(f"Sentinel-2 unavailable ({e}); truth uses shipped crop parameters")
    if anchor and series:
        engine = "Wofost72_PP" if a.irrigated else "Wofost72_WLP_CWB"
        cfg = ZoneConfig("anchor", lat, lon, a.crop, a.variety, sow, harvest, soil=soil)
        overrides, info = anchor_to_cover(cfg, season, series, members=24, seed=seed, engine=engine)

    spec = FarmSpec(archetype=a, site=site, year=year, polygon_wgs84=polygon_wgs84, soil=soil,
                    elevation_m=elev, region=a.region, cell_m=cell_m, seed=seed,
                    weed_mean_m2=weed_mean_m2 if a.weeds else 0.0, blight_inoculum=blight_inoculum,
                    twi_cells=twi_cells, param_overrides=overrides, n_zones=n_zones, provenance=prov)
    return VirtualFarm(spec, season, history, series, info, prov, warns)
