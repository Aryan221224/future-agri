"""HTTP surface for the virtual farm, the spray-drone twin and the runner.

Building a farm fetches real data (cached afterwards) and a season run takes
minutes, so these are synchronous research endpoints, not a high-throughput
service; the CLI (``python -m fa_runner``) is the batch path.
"""
from __future__ import annotations

import dataclasses
import threading
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["farm", "drone"])
_FARMS: dict[str, Any] = {}
_LOCK = threading.Lock()


class BuildRequest(BaseModel):
    archetype: str = Field(..., examples=["A5"])
    year: int = Field(..., examples=[2025])
    site: int = 0
    cell_m: float = Field(10.0, gt=0.5, le=50)
    seed: int = 0


class FlightModel(BaseModel):
    height_m: float = Field(2.5, gt=0.5, le=10)
    speed_ms: float = Field(5.0, gt=0.0, le=10)
    swath_m: float = Field(4.0, gt=0.5, le=10)
    rate_l_ha: float = Field(15.0, gt=1, le=100)
    rtk: bool = False
    pump_lpm: float = Field(3.5, gt=0, le=10)

    def settings(self):
        from fa_drone.mission import MissionSettings
        return MissionSettings(**self.model_dump())


class WeatherModel(BaseModel):
    wind_ms: float = Field(2.0, ge=0, le=40)
    wind_dir_deg: float = Field(270.0, ge=0, le=360)
    gust_ms: float = Field(3.5, ge=0, le=60)
    temp_c: float = Field(20.0, ge=-40, le=60)


class SimulateRequest(BaseModel):
    polygon: dict
    flight: FlightModel = FlightModel()
    weather: WeatherModel = WeatherModel()
    region: str = "GLOBAL"
    cell_m: float = Field(5.0, gt=0.5, le=50)
    seed: int = 0


class SeasonRequest(BaseModel):
    policy: str = "twin"
    seed: int = 0
    flight: FlightModel = FlightModel()
    plan_members: int = Field(10, ge=4, le=51)


class JobRecordsRequest(BaseModel):
    text: str
    mapping: dict | None = None


def _asdict(x):
    from fa_runner.report import _default
    import json
    return json.loads(json.dumps(x, default=_default))


@router.get("/farm/archetypes")
def archetypes():
    from fa_farm.archetypes import load_archetypes
    return {k: {"name": a.name, "crop": a.crop, "region": a.region, "supported": a.supported,
                "unsupported_reason": a.unsupported_reason, "field_ha": a.field_ha,
                "sites": [dataclasses.asdict(s) for s in a.locations]}
            for k, a in load_archetypes().items()}


@router.get("/farm/regions")
def regions():
    from fa_farm.regions import list_regions, load_region
    return {r: {"name": load_region(r).name, "currency": load_region(r).currency,
                "verified": load_region(r).rules.verified} for r in list_regions()}


@router.post("/farm/build")
def build(req: BuildRequest):
    from fa_farm.archetypes import load_archetypes
    from fa_farm.build import build_virtual_farm
    from fa_farm.sources.base import DataUnavailable
    if req.archetype not in load_archetypes():
        raise HTTPException(404, f"unknown archetype {req.archetype}")
    try:
        vf = build_virtual_farm(req.archetype, req.year, site_index=req.site, cell_m=req.cell_m,
                                seed=req.seed)
    except (ValueError, DataUnavailable) as e:
        raise HTTPException(422, str(e)) from e
    fid = uuid.uuid4().hex[:12]
    with _LOCK:
        _FARMS[fid] = vf
    return {"farm_id": fid, **_asdict(vf.summary())}


def _farm(fid: str):
    vf = _FARMS.get(fid)
    if vf is None:
        raise HTTPException(404, f"no farm {fid}")
    return vf


@router.post("/farm/{fid}/season")
def season(fid: str, req: SeasonRequest):
    from fa_runner.season import POLICIES, run_season
    if req.policy not in POLICIES:
        raise HTTPException(422, f"policy must be one of {POLICIES}")
    r = run_season(_farm(fid), req.policy, seed=req.seed, flight=req.flight.settings(),
                   plan_members=req.plan_members)
    return _asdict(r.summary())


@router.post("/drone/simulate")
def simulate(req: SimulateRequest):
    from fa_drone.mission import Weather, simulate_mission
    from fa_drone.spec import load_spec
    from fa_farm.grid import FarmGrid
    from fa_farm.regions import load_region
    from fa_farm.sources.boundaries import polygon_from_geojson
    try:
        poly = polygon_from_geojson(req.polygon)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(422, f"bad polygon: {e}") from e
    grid = FarmGrid.from_wgs84(poly, req.cell_m)
    if grid.area_ha > 500:
        raise HTTPException(422, "field larger than 500 ha: split it or use the CLI")
    w = req.weather
    r = simulate_mission(grid, load_spec(), req.flight.settings(),
                         Weather(w.wind_ms, w.wind_dir_deg, w.gust_ms, w.temp_c),
                         rules=load_region(req.region).rules, seed=req.seed)
    return {"feasible": r.feasible, "reasons": r.reasons, "kpis": r.kpis, "warnings": r.warnings,
            "telemetry": r.telemetry}


@router.post("/drone/kml")
def kml(req: SimulateRequest):
    import tempfile
    from pathlib import Path
    from fa_drone.jiyi_io import export_kml, settings_sheet
    from fa_drone.spec import load_spec
    from fa_farm.regions import load_region
    from fa_farm.sources.boundaries import polygon_from_geojson
    poly = polygon_from_geojson(req.polygon)
    with tempfile.TemporaryDirectory() as d:
        rep: dict = {}
        p = export_kml(poly, [], "field", Path(d) / "f.kml", report=rep)
        txt = p.read_text()
    return {"kml": txt, "report": rep,
            "settings": _asdict(settings_sheet(req.flight.settings(), load_spec(), load_region(req.region)))}


@router.post("/drone/job-records")
def job_records(req: JobRecordsRequest):
    from fa_drone.jiyi_io import import_job_records
    try:
        recs, errors = import_job_records(req.text, req.mapping)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, f"unreadable job records: {e}") from e
    return {"records": _asdict(recs), "errors": errors}
