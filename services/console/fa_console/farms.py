"""Farms: create, build (virtual test farm | live field), load, delete, boundary detection."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import pickle
import threading
from collections import OrderedDict

from fa_core.geo import area_ha
from fa_farm.archetypes import load_archetypes, season_dates
from fa_farm.build import build_field_farm, build_virtual_farm, with_region
from fa_farm.regions import list_regions, load_region
from fa_farm.sources.base import DataUnavailable
from fa_farm.sources.boundaries import candidates_near, field_near, polygon_from_geojson

from .errors import Busy, NotFound
from .scene import build_scene, imagery_png
from .store import now

MAX_HA = 500.0
MAX_CELLS = 20_000
EEA = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
       "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO"}


def region_for_country(cc: str | None) -> str:
    cc = (cc or "").upper()
    if cc in EEA:
        return "EU"
    return cc if cc in list_regions() else "GLOBAL"


def default_sim_year(a, today: dt.date) -> int:
    for y in range(today.year, today.year - 8, -1):
        _, harv = season_dates(a, y)
        if harv + dt.timedelta(days=5) <= today:
            return y
    return today.year - 2


def cap_cells(area: float, cell_m: float) -> tuple[float, str | None]:
    n = area * 1e4 / cell_m ** 2
    if n <= MAX_CELLS:
        return cell_m, None
    new = math.ceil(math.sqrt(area * 1e4 / MAX_CELLS) * 2) / 2
    return new, (f"cell size raised from {cell_m:g} m to {new:g} m to keep the farm under "
                 f"{MAX_CELLS:,} cells")


class FarmService:
    def __init__(self, store, settings, jobs):
        self.store, self.settings, self.jobs = store, settings, jobs
        self.builder = build_virtual_farm
        self.field_builder = build_field_farm
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.today = dt.date.today
        jobs.register("build", self.build_job)

    # ---------------------------------------------------------------- create
    def template(self, tid: str):
        a = load_archetypes().get(tid)
        if a is None:
            raise ValueError(f"unknown template '{tid}'")
        if not a.supported:
            raise ValueError(f"template {a.id} ({a.name}) is not supported: {a.unsupported_reason}")
        return a

    def create(self, req: dict) -> dict:
        a = self.template(req.get("template") or "A5")
        mode = req.get("mode") or "sim"
        if mode not in ("sim", "field"):
            raise ValueError("mode must be 'sim' (virtual test farm) or 'field' (live field)")
        today = self.today()
        warnings: list[str] = []
        area = None
        country = (req.get("country") or "").upper() or None
        polygon = req.get("polygon")
        site_index = req.get("site_index")
        if polygon:
            g = polygon_from_geojson(polygon)
            area = area_ha(g)
            if area > MAX_HA:
                raise ValueError(f"the field is {area:.0f} ha; the console handles fields up to {MAX_HA:.0f} ha")
            if area < 0.01:
                raise ValueError("the field is smaller than 100 m2")
            lat, lon = float(g.centroid.y), float(g.centroid.x)
            polygon = json.loads(json.dumps(g.__geo_interface__))
        elif req.get("lat") is not None and req.get("lon") is not None:
            if not country:
                raise ValueError("a country (ISO code) is needed to find the field boundary at a point")
            lat, lon = float(req["lat"]), float(req["lon"])
        else:
            idx = site_index or 0
            if not 0 <= idx < len(a.locations):
                raise ValueError(f"template {a.id} has {len(a.locations)} example sites")
            site = a.locations[idx]
            lat, lon, country = site.lat, site.lon, site.country
            site_index = idx
        region = req.get("region") or (region_for_country(country) if country else a.region)
        if region not in list_regions():
            raise ValueError(f"unknown region pack '{region}'")
        if (lat < 0) != (a.locations[0].lat < 0):
            warnings.append(f"the {a.id} crop calendar is from the other hemisphere; check the season dates")
        if mode == "sim":
            year = int(req.get("year") or default_sim_year(a, today))
            sow, harv = season_dates(a, year)
            if harv + dt.timedelta(days=4) > today:
                raise ValueError(f"the {year} season (harvest {harv}) is not complete yet; pick an earlier "
                                 "year, or create a live field")
            if year < 2001:
                raise ValueError("weather records start in 2001")
        else:
            if not req.get("sowing"):
                raise ValueError("a live field needs its sowing (or transplanting) date")
            sow = req["sowing"] if isinstance(req["sowing"], dt.date) else dt.date.fromisoformat(str(req["sowing"]))
            if sow < today - dt.timedelta(days=400) or sow > today + dt.timedelta(days=120):
                raise ValueError("the sowing date must be within the last 400 days or the next 120")
            t_sow, t_harv = season_dates(a, sow.year)
            harv, year = sow + (t_harv - t_sow), sow.year
        cell_m = float(req.get("cell_m") or 10.0)
        if area is not None:
            cell_m, w = cap_cells(area, cell_m)
            if w:
                warnings.append(w)
        farm = self.store.create_farm({
            "name": req["name"], "mode": mode, "template": a.id, "crop": a.crop, "region": region,
            "country": country, "year": year, "lat": lat, "lon": lon, "area_ha": area, "cell_m": cell_m,
            "polygon": polygon, "sowing": sow.isoformat(), "harvest": harv.isoformat(), "status": "queued",
            "meta": {"seed": int(req.get("seed") or 0), "site_index": site_index, "warnings": warnings}})
        self.store.add_event(farm["id"], "farm_created", {"name": farm["name"], "mode": mode, "template": a.id})
        job = self.jobs.submit("build", farm["id"], {}, target="local")
        return {"farm": self.store.get_farm(farm["id"]), "job": job}

    # ---------------------------------------------------------------- build
    def build_job(self, job: dict, ctx) -> dict:
        fid = job["farm_id"]
        farm = self.store.get_farm(fid)
        if farm is None:
            raise NotFound(f"no farm {fid}")
        meta = dict(farm["meta"])
        self.store.update_farm(fid, status="building")
        try:
            return self._build(fid, farm, meta, ctx)
        except Exception as e:
            meta["error"] = f"{type(e).__name__}: {e}"
            self.store.update_farm(fid, status="error", meta=meta)
            self.store.add_event(fid, "farm_build_failed", {"error": meta["error"]})
            raise

    def _build(self, fid: str, farm: dict, meta: dict, ctx) -> dict:
        a = self.template(farm["template"])
        cache = self.settings.data_cache()
        warnings = list(meta.get("warnings", []))
        cell = float(farm["cell_m"])
        ctx.progress(0.02, "starting")
        if farm["polygon"]:
            poly = polygon_from_geojson(farm["polygon"])
        else:
            if self.settings.offline:
                raise DataUnavailable("field boundaries", "offline: upload the field boundary instead")
            ctx.progress(0.04, "finding the field boundary (Fields of The World)")
            poly, attrs, prov = field_near(farm["lat"], farm["lon"], farm["country"], cache, *a.field_ha)
            area = area_ha(poly)
            if area > MAX_HA:
                raise ValueError(f"the detected field is {area:.0f} ha (limit {MAX_HA:.0f} ha); upload a boundary")
            cell, w = cap_cells(area, cell)
            if w:
                warnings.append(w)
            meta["boundary"] = attrs
            self.store.update_farm(fid, polygon=json.loads(json.dumps(poly.__geo_interface__)), area_ha=area,
                                   lat=float(poly.centroid.y), lon=float(poly.centroid.x), cell_m=cell)

        step = {"step": None, "started": None}

        def progress(frac, msg, detail=None):
            # the smaller loader: a new step restarts it; counts/rate/eta come from the step itself
            if msg != step["step"]:
                step.update(step=msg, started=now())
            ctx.progress(0.05 + 0.8 * float(frac), msg, {**step, **(detail or {})})

        if farm["mode"] == "sim":
            vf = self.builder(farm["template"], int(farm["year"]), cache,
                              site_index=meta.get("site_index") or 0, cell_m=cell, seed=meta.get("seed", 0),
                              polygon_wgs84=poly, progress=progress)
            if farm["region"] and farm["region"] != vf.spec.archetype.region:
                vf.spec = dataclasses.replace(vf.spec, archetype=with_region(vf.spec.archetype, farm["region"]),
                                              region=farm["region"])
        else:
            vf = self.field_builder(farm["template"], poly, dt.date.fromisoformat(farm["sowing"]), cache,
                                    cell_m=cell, region=farm["region"], seed=meta.get("seed", 0),
                                    country=farm["country"] or "", progress=progress)
        ctx.progress(0.86, "saving the digital farm")
        self._save(fid, vf)
        ctx.progress(0.9, "building the 3D scene")
        region = load_region(farm["region"] or a.region)
        scene = build_scene(vf, cache, offline=self.settings.offline, mode=farm["mode"],
                            buffer_m=float(region.rules.buffer_m or 0.0))
        try:
            png, imeta = imagery_png(vf, cache, offline=self.settings.offline, mode=farm["mode"])
            (self.store.farm_dir(fid) / "imagery.png").write_bytes(png)
            scene["imagery"] = {"available": True, "url": f"/api/farms/{fid}/imagery.png", **imeta}
        except DataUnavailable as e:
            scene["imagery"] = {"available": False, "reason": str(e)}
        (self.store.farm_dir(fid) / "scene.json").write_text(json.dumps(scene))
        summ = vf.summary()
        for w in list(getattr(vf, "warnings", [])) + scene["warnings"]:
            if w not in warnings:
                warnings.append(w)
        sow, harv = season_dates(vf.spec.archetype, vf.spec.year)
        meta.update(warnings=warnings, error=None, provenance=summ.pop("provenance", []), summary=summ)
        self.store.update_farm(fid, status="ready", area_ha=summ["area_ha"], lat=summ["lat"], lon=summ["lon"],
                               cell_m=vf.spec.cell_m, crop=summ["crop"], sowing=sow.isoformat(),
                               harvest=harv.isoformat(), meta=meta)
        self.store.add_event(fid, "farm_built", {"cells": summ["cells"], "area_ha": summ["area_ha"]})
        ctx.progress(1.0, "ready")
        return {"summary": {"cells": summ["cells"], "area_ha": summ["area_ha"], "warnings": len(warnings)}}

    # ---------------------------------------------------------------- load / delete
    def _save(self, fid: str, vf) -> None:
        p = self.store.farm_dir(fid) / "farm.pkl"
        tmp = p.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(vf, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(p)
        with self._lock:
            self._cache[fid] = vf
            self._cache.move_to_end(fid)
            while len(self._cache) > 6:
                self._cache.popitem(last=False)

    def load(self, fid: str):
        with self._lock:
            if fid in self._cache:
                self._cache.move_to_end(fid)
                return self._cache[fid]
        farm = self.get(fid)
        p = self.store.farm_dir(fid) / "farm.pkl"
        if not p.exists():
            raise NotFound(f"farm {farm['name']} is not built yet (status {farm['status']})")
        with open(p, "rb") as fh:
            vf = pickle.load(fh)
        with self._lock:
            self._cache[fid] = vf
        return vf

    def get(self, fid: str) -> dict:
        f = self.store.get_farm(fid)
        if f is None:
            raise NotFound(f"no farm {fid}")
        return f

    def scene(self, fid: str) -> dict:
        self.get(fid)
        p = self.store.farm_dir(fid) / "scene.json"
        if not p.exists():
            raise NotFound("the 3D scene is built with the farm; the farm is not ready yet")
        return json.loads(p.read_text())

    def delete(self, fid: str) -> None:
        self.get(fid)
        if any(j["status"] == "running" for j in self.store.list_jobs(fid, 200)):
            raise Busy("a job is running for this farm; cancel it first")
        with self._lock:
            self._cache.pop(fid, None)
        self.store.delete_farm(fid)

    # ---------------------------------------------------------------- boundary detection
    def detect_boundary(self, lat: float, lon: float, country: str, min_ha: float = 0.05,
                        max_ha: float = MAX_HA, limit: int = 12) -> dict:
        import shapely
        from shapely.geometry import Point
        cache = self.settings.data_cache()
        cands = candidates_near(lat, lon, country.upper(), cache, 0.01)
        pt = Point(lon, lat)
        out = []
        for c in cands:
            a = c["area_m2"] / 1e4
            if not (min_ha <= a <= max_ha):
                continue
            g = shapely.from_wkb(bytes.fromhex(c["wkb_hex"]))
            if g.geom_type == "MultiPolygon":
                g = max(g.geoms, key=lambda p: p.area)
            out.append((g.distance(pt), g, c, a))
        out.sort(key=lambda t: t[0])
        feats = [{"type": "Feature", "geometry": json.loads(json.dumps(g.__geo_interface__)),
                  "properties": {"id": c["id"], "area_ha": round(a, 3), "confidence": c["confidence"],
                                 "contains_point": bool(g.contains(pt))}}
                 for _, g, c, a in out[:limit]]
        return {"type": "FeatureCollection", "features": feats,
                "attribution": "Fields of The World (CC-BY-4.0)"}
