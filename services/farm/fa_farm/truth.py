"""The ground-truth farm: the "real farm" of a virtual test.

Built from real data (boundary, soil, terrain, weather, satellite history) plus
within-field structure, and stepped day by day with the *observed* weather. The
twin never touches this object: it only receives scans of it (``fa_scan``) and
reports of what was sprayed. That separation is what makes a closed-loop run a
test rather than a self-consistency check.

Physics, per cell:
* crop growth -- WOFOST per soil zone (water-limited, or potential for
  irrigated crops), scaled by a patchy emergence factor;
* late blight -- the project's epidemic model per cell, onset spreading from
  foci, protectant efficacy set by the dose actually deposited there; the
  zone-mean leaf loss is fed back into that zone's WOFOST (as ``run_with_damage``);
* weeds -- Cousens loss from the mean density over the crop's critical period of
  weed control, after whatever herbicide actually landed on the cell;
* yield -- zone yield x (cell green-area duration / zone mean) x emergence x
  (1 - weed loss).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from fa_twin.damage import (BlightParams, WeedParams, dose_efficacy, favourable, infection_day,
                            weed_loss_frac)
from fa_twin.engine import SoilParams, ZoneConfig, ZoneTwin
from fa_twin.weather import records_provider

from .archetypes import Archetype, Site, season_dates
from .grid import FarmGrid
from .heterogeneity import emergence_field, foci_onset, soil_zones, weed_seedbank

K_COVER = 0.6

#: Critical period of weed control, days after sowing/transplanting (start, end):
#: weeds present in this window cost yield; earlier they are too small to compete,
#: later the crop has closed. Concept and data: Knezevic et al. (2002) Weed Science
#: 50:773-786; crop windows here are typical published ranges (assumption per crop).
CPWC_DAYS = {"potato": (14, 49), "maize": (21, 63), "rice": (14, 56), "soybean": (14, 49),
             "wheat": (21, 56)}
CPWC_DEFAULT = (14, 56)


@dataclass
class FarmSpec:
    archetype: Archetype
    site: Site
    year: int
    polygon_wgs84: object
    soil: SoilParams
    elevation_m: float
    region: str
    cell_m: float = 5.0
    seed: int = 0
    weed_mean_m2: float = 8.0
    blight_inoculum: bool | None = None
    twi_cells: np.ndarray | None = None
    param_overrides: dict = field(default_factory=dict)
    n_zones: int = 4
    provenance: list = field(default_factory=list)
    #: Pre-season field science (fa_farm.science): knowable before sowing, so the twin gets it too.
    #: ``param_overrides`` above is the satellite anchor -- the truth's own, never the twin's.
    science_overrides: dict = field(default_factory=dict)
    wav_fraction: float | None = None        # soil water at sowing, fraction of plant-available capacity
    soil_evap_factor: float = 1.0
    science_notes: list = field(default_factory=list)

    @property
    def centroid(self) -> tuple[float, float]:
        c = self.polygon_wgs84.centroid
        return float(c.y), float(c.x)

    def wav_for(self, soil: SoilParams) -> float:
        """Initial available water (cm) for a zone's soil; WOFOST's 25 cm without a spin-up."""
        if self.wav_fraction is None:
            return 25.0
        return round(self.wav_fraction * max(0.0, soil.SMFCF - soil.SMW) * soil.RDMSOL, 2)

    def science_inputs(self) -> dict:
        """What the twin may know: soil prior, soil water at sowing, management/hybrid overrides."""
        return {"soil": self.soil, "WAV": self.wav_for(self.soil),
                "overrides": dict(getattr(self, "science_overrides", None) or {}),   # farms pickled before 0.4
                "soil_evap_factor": self.soil_evap_factor}


@dataclass
class CellState:
    day: dt.date
    lai: np.ndarray
    cc: np.ndarray
    dvs: np.ndarray
    weeds_m2: np.ndarray
    blight_sev: np.ndarray
    zone: np.ndarray


@dataclass
class AsApplied:
    """What actually landed: dose per cell as a fraction of the label dose."""
    day: dt.date
    product: str                     # "fungicide" | "herbicide"
    dose_frac: np.ndarray
    off_target_l: float = 0.0
    litres: float = 0.0


@dataclass
class HarvestResult:
    yield_kg_ha_cells: np.ndarray
    field_yield_kg_ha: float
    zone_yield_kg_ha: list[float]
    weed_loss_mean: float
    blight_final_mean: float
    blight_infection_day: dt.date | None


class GroundTruthFarm:
    def __init__(self, spec: FarmSpec, weather_records: list[dict],
                 cpwc_days: tuple[int, int] | None = None,
                 blight_params: BlightParams | None = None, weed_params: WeedParams | None = None):
        a = spec.archetype
        if not a.supported:
            raise ValueError(f"archetype {a.id} not supported: {a.unsupported_reason}")
        self.spec = spec
        rng = np.random.default_rng(spec.seed)
        self.grid = FarmGrid.from_wgs84(spec.polygon_wgs84, spec.cell_m)
        n = self.grid.n_cells
        twi = spec.twi_cells if (spec.twi_cells is not None and len(spec.twi_cells) == n) else None
        k = max(1, min(spec.n_zones, n // 4 if n >= 4 else 1))
        self.zone, self.zone_soils = soil_zones(self.grid, spec.soil, twi, rng, k=k)
        self.emergence = emergence_field(self.grid, rng)
        self.wp = weed_params or WeedParams()
        self.weeds0 = (weed_seedbank(self.grid, rng, spec.weed_mean_m2) if a.weeds
                       else np.zeros(n))
        self.weeds = self.weeds0.copy()
        self.cpwc = cpwc_days or CPWC_DAYS.get(a.crop, CPWC_DEFAULT)
        self._comp_sum = np.zeros(n)
        self._comp_days = 0

        self.sowing, self.harvest_date = season_dates(a, spec.year)
        self.records = sorted(weather_records, key=lambda r: r["DAY"])
        self._by_day = {r["DAY"]: r for r in self.records}
        missing = [d for d in (self.sowing, self.harvest_date) if d not in self._by_day]
        if missing:
            raise ValueError(f"weather does not cover the season ({missing[0]} missing)")
        lat, lon = spec.centroid
        self.weather = records_provider(self.records, lat, lon, spec.elevation_m,
                                        "observed weather (truth)", soil_evap_factor=spec.soil_evap_factor)
        self.engine = "Wofost72_PP" if a.irrigated else "Wofost72_WLP_CWB"
        self.zones: list[ZoneTwin] = []
        for i, soil in enumerate(self.zone_soils):
            cfg = ZoneConfig(f"truth-z{i}", lat, lon, a.crop, a.variety, self.sowing,
                             self.harvest_date, soil=soil, WAV=spec.wav_for(soil),
                             soil_evap_factor=spec.soil_evap_factor,
                             param_overrides={**(getattr(spec, "science_overrides", None) or {}),
                                              **spec.param_overrides})
            self.zones.append(ZoneTwin(cfg, self.weather, engine=self.engine))
        self._zone_lai = np.zeros(len(self.zones))
        self._zone_dvs = np.zeros(len(self.zones))
        self._zone_done = [False] * len(self.zones)

        self.bp = blight_params or BlightParams()
        self.blight_modelled = a.crop == "potato" and "late_blight" in a.diseases
        self.blight_infection_day = (infection_day(self.records, self.sowing, rng, self.bp,
                                                   inoculum=spec.blight_inoculum)
                                     if self.blight_modelled else None)
        if self.blight_infection_day is not None:
            n_foci = 1 + int(rng.poisson(0.5 * self.grid.area_ha))
            self._onset = foci_onset(self.grid, rng, n_foci)
        else:
            self._onset = None
        self.S = np.zeros(n)
        self._prot_until = np.full(n, -1, dtype=np.int64)
        self._prot_eff = np.zeros(n)
        self._gad = np.zeros(n)
        self.day = self.sowing - dt.timedelta(days=1)
        self.as_applied_log: list[AsApplied] = []

    # ------------------------------------------------------------------ stepping
    def advance_to(self, day: dt.date) -> None:
        stop = min(day, self.harvest_date)
        while self.day < stop:
            d = self.day + dt.timedelta(days=1)
            rec = self._by_day.get(d)
            if rec is None:
                raise ValueError(f"no observed weather for {d}")
            dS = self._step_blight(d, rec)
            for i, zt in enumerate(self.zones):
                if self._zone_done[i]:
                    continue
                m = zt.model
                m.run(days=1)
                if m.flag_terminate:
                    self._zone_done[i] = True
                    self._zone_lai[i] = 0.0
                    continue
                lai = float(m.get_variable("LAI") or 0.0)
                sel = self.zone == i
                if dS is not None and lai > 0:
                    dsm = float(dS[sel].mean())
                    if dsm > 0:
                        healthy = max(1e-3, 1.0 - float(self.S[sel].mean()) + dsm)
                        lai = max(0.01, lai * (1.0 - dsm / healthy))
                        m.set_variable("LAI", lai)
                self._zone_lai[i] = lai
                self._zone_dvs[i] = float(m.get_variable("DVS") or 0.0)
            self._gad += self._zone_lai[self.zone] * (1.0 - self.S)
            if self.cpwc[0] <= (d - self.sowing).days <= self.cpwc[1]:
                self._comp_sum += self.weeds
                self._comp_days += 1
            self.day = d

    def _step_blight(self, d: dt.date, rec: dict) -> np.ndarray | None:
        if self.blight_infection_day is None:
            return None
        if rec["RAIN"] >= self.bp.washoff_mm:
            self._prot_until[:] = -1
        if d < self.blight_infection_day:
            return np.zeros_like(self.S)
        since = (d - self.blight_infection_day).days
        active = since >= self._onset
        new = active & (self.S == 0.0)
        grow = active & (self.S > 0.0)
        r = self.bp.r_fav if favourable(rec) else self.bp.r_unfav
        protected = self._prot_until >= d.toordinal()
        rate = np.where(protected, r * (1.0 - self._prot_eff), r)
        S_new = self.S.copy()
        S_new[new] = self.bp.s0
        S_new[grow] = np.minimum(0.999, self.S[grow] + rate[grow] * self.S[grow] * (1 - self.S[grow]))
        dS = S_new - self.S
        self.S = S_new
        return dS

    # ------------------------------------------------------------------ views
    def state(self, day: dt.date | None = None) -> CellState:
        if day is not None:
            self.advance_to(day)
        lai = self._zone_lai[self.zone] * self.emergence * (1.0 - self.S)
        return CellState(self.day, lai, 1.0 - np.exp(-K_COVER * lai), self._zone_dvs[self.zone].copy(),
                         self.weeds.copy(), self.S.copy(), self.zone.copy())

    # ------------------------------------------------------------------ treatments
    def apply(self, aa: AsApplied) -> None:
        """Apply what the drone actually deposited (per-cell dose fraction)."""
        if len(aa.dose_frac) != self.grid.n_cells:
            raise ValueError("dose_frac must have one value per grid cell")
        self.advance_to(aa.day)
        if aa.product == "herbicide":
            eff = dose_efficacy(aa.dose_frac, self.wp.herbicide_efficacy)
            self.weeds = self.weeds * (1.0 - eff)
        elif aa.product == "fungicide":
            eff = dose_efficacy(aa.dose_frac, self.bp.efficacy)
            hit = eff > 1e-3
            until = aa.day.toordinal() + self.bp.protect_days
            still = self._prot_until >= aa.day.toordinal()
            self._prot_eff = np.where(hit, np.where(still, np.maximum(self._prot_eff, eff), eff),
                                      self._prot_eff)
            self._prot_until = np.where(hit, until, self._prot_until)
        else:
            raise ValueError(f"unknown product {aa.product!r}")
        self.as_applied_log.append(aa)

    # ------------------------------------------------------------------ outcome
    def harvest(self) -> HarvestResult:
        self.advance_to(self.harvest_date)
        zy = []
        for zt in self.zones:
            m = zt.model
            if not m.flag_terminate:
                m.run_till_terminate()
            out = m.get_summary_output() or [{}]
            zy.append(float(out[0].get("TWSO") or 0.0))
        zy_arr = np.array(zy)
        gad_ratio = np.ones(self.grid.n_cells)
        for i in range(len(self.zones)):
            sel = self.zone == i
            mean = self._gad[sel].mean()
            if mean > 0:
                gad_ratio[sel] = self._gad[sel] / mean
        dens = self._comp_sum / self._comp_days if self._comp_days else self.weeds
        wl = np.array([weed_loss_frac(x, self.wp) for x in dens])
        cell_y = zy_arr[self.zone] * gad_ratio * self.emergence * (1.0 - wl)
        return HarvestResult(cell_y, float(cell_y.mean()), zy, float(wl.mean()),
                             float(self.S.mean()), self.blight_infection_day)
