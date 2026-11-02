"""The twin as it runs on a virtual (or real) farm: it knows the location, the
crop, the region's prices and rules, the observed weather so far, a soil
*prior* (SoilGrids) and whatever the scans and job records told it -- never the
ground truth. Crop state and the blight plan are field-level (the validated
FieldTwin and weekly planner); weeds are mapped per cell from the scan, which is
what spot spraying needs.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from fa_loop.orchestrator import Decision, FieldTwin, TwinSettings
from fa_twin.engine import SoilParams, ZoneConfig

from .economics import Economics


class TwinFarm:
    def __init__(self, archetype, region, lat: float, lon: float, observed_records: list[dict],
                 soil_prior: SoilParams, sowing: dt.date, harvest: dt.date,
                 inoculum: bool | None = None, objective: str = "mean", plan_members: int = 10,
                 expected_yield_kg_ha: float | None = None):
        self.archetype = archetype
        self.region = region
        self.econ = Economics.from_region(region, archetype.crop)
        engine = "Wofost72_PP" if archetype.irrigated else "Wofost72_WLP_CWB"
        cfg = ZoneConfig("twin", lat, lon, archetype.crop, archetype.variety, sowing, harvest,
                         soil=soil_prior)
        settings = TwinSettings(econ=self.econ.planner_econ(), objective=objective,
                                plan_members=plan_members,
                                herbicide_crops=frozenset({archetype.crop}) if archetype.weeds
                                else frozenset(),
                                expected_yield_kg_ha=expected_yield_kg_ha)
        self.field = FieldTwin(cfg, observed_records, settings=settings, inoculum=inoculum,
                               engine=engine)
        self.digital = None

    # -- observations -------------------------------------------------------
    def add_weather(self, records: list[dict]) -> int:
        return self.field.add_weather(records)

    def ingest(self, digital, day: dt.date, members: int = 16, seed: int = 0) -> Decision:
        """Assimilate a scan's DigitalFarm (area-weighted cover and stage) and
        decide herbicide from its per-cell weed map."""
        prev = getattr(self, "digital", None)
        if prev is not None or not np.isfinite(digital.weed_map).all():
            digital = self._merge(prev, digital)
        self.digital = digital
        if not getattr(digital, "crop_observed", True):
            return self._weeds_only(digital, day)
        # A herbicide only saves the part of the critical period still ahead of it.
        from dataclasses import replace
        from fa_farm.truth import CPWC_DAYS, CPWC_DEFAULT
        lo, hi = CPWC_DAYS.get(self.archetype.crop, CPWC_DEFAULT)
        das_fly = (day - self.field.cfg.sowing_date).days + 2
        remaining = float(np.clip((hi - max(das_fly, lo)) / (hi - lo), 0.0, 1.0))
        base = getattr(self, "_base_weeds", None) or self.field.s.weeds
        self._base_weeds = base
        self.field.s.weeds = replace(base, herbicide_efficacy=base.herbicide_efficacy * remaining)
        a = np.array([z.area_ha for z in digital.zones])
        w = a / a.sum()
        cc = float(np.sum(w * [z.cc for z in digital.zones]))
        dvs = float(np.sum(w * [z.dvs for z in digital.zones]))
        cc_sd = max(0.04, float(np.sqrt(np.sum((w * [z.cc_sd for z in digital.zones]) ** 2))))
        weeds = np.where(np.isfinite(digital.weed_map), digital.weed_map, 0.0)
        return self.field.ingest_pass(day, cc, dvs, weeds if self.archetype.weeds else None,
                                      canopy_cover_sd=cc_sd, members=members, seed=seed)

    def _weeds_only(self, digital, day: dt.date) -> Decision:
        """A scan that measured weeds but not cover or stage: re-decide herbicide on the merged
        map; the crop model is left alone (an uninformative cover/stage must not move it)."""
        from fa_planner.weekly import herbicide_decision
        f = self.field
        if self.archetype.weeds and f.cfg.crop in f.s.herbicide_crops:
            f.herbicide = herbicide_decision(np.asarray(digital.weed_map, float), f._expected_yield_hint(),
                                             f.s.econ, f.s.weeds)
        return Decision(day, "pass", True, "weed map updated (cover and stage not observed)", {})

    @staticmethod
    def _merge(prev, new):
        """Cells the new scan did not see keep the previous map; with no previous map, the mean
        of what this scan saw (the best prior there is) -- never zero."""
        from dataclasses import replace
        seen = np.isfinite(new.weed_map)
        if seen.all():
            return new
        if prev is not None:
            fill, fill_sd = prev.weed_map, prev.weed_map_sd
        else:
            mu = float(np.nanmean(new.weed_map)) if seen.any() else 0.0
            fill, fill_sd = np.full(len(seen), mu), np.full(len(seen), np.inf)
        return replace(new, weed_map=np.where(seen, new.weed_map, fill),
                       weed_map_sd=np.where(seen, new.weed_map_sd, fill_sd))

    def herbicide_targets(self, weed_map: np.ndarray) -> tuple[str, np.ndarray]:
        """The planner's herbicide rule (none / blanket / spot) applied to a weed map."""
        from fa_planner.weekly import herbicide_decision
        w = np.asarray(weed_map, float)
        f = self.field
        dec = herbicide_decision(w, f._expected_yield_hint(), f.s.econ, f.s.weeds)
        if dec.option == "blanket":
            return "blanket", np.ones(len(w), bool)
        if dec.option == "spot":
            return "spot", np.asarray(w > dec.threshold_m2, bool)
        return "none", np.zeros(len(w), bool)

    # -- decisions ------------------------------------------------------------
    @property
    def herbicide_option(self) -> str:
        h = self.field.herbicide
        return h.option if h is not None else "none"

    def weed_targets(self) -> np.ndarray:
        """Cells to treat: all (blanket), above the economic threshold (spot), or none."""
        if self.digital is None:
            raise RuntimeError("no scan ingested yet")
        n = len(self.digital.weed_map)
        opt = self.herbicide_option
        if opt == "blanket":
            return np.ones(n, bool)
        if opt == "spot":
            return np.asarray(self.digital.weed_map > self.field.herbicide.threshold_m2, bool)
        return np.zeros(n, bool)

    def weekly_plan(self, day: dt.date, forecast: list[list[dict]], seed: int = 0) -> dict:
        return self.field.weekly_plan(day, forecast, seed=seed)

    def record_spray(self, day: dt.date) -> None:
        self.field.record_spray(day)

    def record_scouting(self, day: dt.date, severity: float | None) -> None:
        self.field.record_scouting(day, severity)

    def expected_yield(self) -> float:
        return float(self.field._expected_yield_hint())
