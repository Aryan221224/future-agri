"""Live feeds and field devices."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..devices import DeviceService
from ..live import LiveService

router = APIRouter(prefix="/api", tags=["live"])


def init(app) -> None:
    s = app.state
    s.live = LiveService(s.store, s.settings, s.farms, s.sessions)
    s.devices = DeviceService(s.store, s.farms)


class DeviceIn(BaseModel):
    kind: str
    name: str = Field(..., min_length=1, max_length=60)


class IngestIn(BaseModel):
    token: str
    readings: list[dict]


@router.get("/farms/{fid}/live/weather")
def weather(fid: str, request: Request) -> dict:
    return request.app.state.live.weather(fid)


@router.get("/farms/{fid}/live/outlook")
def outlook(fid: str, request: Request) -> dict:
    return request.app.state.live.outlook(fid)


@router.get("/farms/{fid}/live/satellite")
def satellite(fid: str, request: Request) -> dict:
    return request.app.state.live.satellite(fid)


@router.get("/farms/{fid}/live/alerts")
def alerts(fid: str, request: Request) -> dict:
    return request.app.state.live.alerts(fid)


@router.get("/farms/{fid}/devices")
def devices(fid: str, request: Request) -> dict:
    request.app.state.farms.get(fid)
    return {"devices": request.app.state.store.list_devices(fid)}


@router.post("/farms/{fid}/devices")
def add_device(fid: str, body: DeviceIn, request: Request) -> dict:
    return request.app.state.devices.create(fid, body.kind, body.name)


@router.delete("/farms/{fid}/devices/{did}")
def remove_device(fid: str, did: str, request: Request) -> dict:
    request.app.state.devices.delete(fid, did)
    return {"deleted": did}


@router.post("/ingest")
def ingest(body: IngestIn, request: Request) -> dict:
    return request.app.state.devices.ingest(body.token, body.readings)


@router.get("/farms/{fid}/readings")
def readings(fid: str, request: Request, kind: str | None = None, since: str | None = None,
             device_id: str | None = None, limit: int = Query(2000, ge=1, le=20000)) -> dict:
    request.app.state.farms.get(fid)
    return {"readings": request.app.state.store.list_readings(fid, kind, since, device_id, limit)}


@router.get("/farms/{fid}/tracker")
def tracker(fid: str, request: Request) -> dict:
    return request.app.state.devices.tracker(fid)
