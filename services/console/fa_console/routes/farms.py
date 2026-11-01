"""Farms: create (and build), list, inspect, delete, 3D scene, imagery, boundary detection."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..errors import NotFound
from ..farms import FarmService

router = APIRouter(prefix="/api", tags=["farms"])


def init(app) -> None:
    s = app.state
    s.farms = FarmService(s.store, s.settings, s.jobs)


class FarmCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    mode: Literal["sim", "field"] = "sim"
    template: str = "A5"
    site_index: int | None = Field(None, ge=0, le=20)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    country: str | None = Field(None, min_length=2, max_length=3)
    polygon: dict | None = None
    year: int | None = Field(None, ge=2001, le=2100)
    sowing: dt.date | None = None
    region: str | None = None
    cell_m: float = Field(10.0, ge=2, le=50)
    seed: int = Field(0, ge=0, le=1_000_000)


class Boundary(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    country: str = Field(..., min_length=2, max_length=3)
    min_ha: float = Field(0.05, ge=0)
    max_ha: float = Field(500.0, le=500)


def _svc(request: Request) -> FarmService:
    return request.app.state.farms


@router.get("/farms")
def list_farms(request: Request) -> dict:
    return {"farms": request.app.state.store.list_farms()}


@router.post("/farms")
def create_farm(body: FarmCreate, request: Request) -> dict:
    return _svc(request).create(body.model_dump())


@router.get("/farms/{fid}")
def get_farm(fid: str, request: Request) -> dict:
    return _svc(request).get(fid)


@router.patch("/farms/{fid}")
def rename_farm(fid: str, body: dict, request: Request) -> dict:
    svc = _svc(request)
    svc.get(fid)
    name = str(body.get("name", "")).strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("name must be 1-80 characters")
    request.app.state.store.add_event(fid, "farm_renamed", {"name": name})
    return request.app.state.store.update_farm(fid, name=name)


@router.delete("/farms/{fid}")
def delete_farm(fid: str, request: Request) -> dict:
    _svc(request).delete(fid)
    request.app.state.store.add_event(None, "farm_deleted", {"farm": fid})
    return {"deleted": fid}


@router.post("/farms/{fid}/build")
def rebuild(fid: str, request: Request) -> dict:
    svc = _svc(request)
    svc.get(fid)
    request.app.state.store.update_farm(fid, status="queued")
    return request.app.state.jobs.submit("build", fid, {}, target="local")


@router.get("/farms/{fid}/scene")
def scene(fid: str, request: Request) -> dict:
    return _svc(request).scene(fid)


@router.get("/farms/{fid}/imagery.png")
def imagery(fid: str, request: Request):
    _svc(request).get(fid)
    p = request.app.state.store.farm_dir(fid) / "imagery.png"
    if not p.exists():
        raise NotFound("no satellite image for this farm")
    return FileResponse(p, media_type="image/png")


@router.post("/boundary/detect")
def detect(body: Boundary, request: Request) -> dict:
    return _svc(request).detect_boundary(body.lat, body.lon, body.country, body.min_ha, body.max_ha)
