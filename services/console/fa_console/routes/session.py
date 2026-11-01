"""The mission session: the closed loop, day by day, with operator decisions."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..sessions import SessionService

router = APIRouter(prefix="/api/farms/{fid}/session", tags=["mission"])


def init(app) -> None:
    s = app.state
    s.sessions = SessionService(s.store, s.settings, s.farms, s.jobs)


class OpIn(BaseModel):
    op_id: str = Field(..., max_length=80)


class RejectIn(OpIn):
    reason: str = Field("", max_length=300)


class DeferIn(OpIn):
    days: int = Field(..., ge=1, le=14)


class FlyIn(OpIn):
    settings: dict | None = None


class RunIn(BaseModel):
    until: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    stop_on_decision: bool = True
    approve_all: bool = False


class ScanIn(BaseModel):
    gsd_m: float | None = Field(None, ge=0.001, le=0.1)


class UploadIn(BaseModel):
    geojson: dict
    gsd_m: float = Field(0.005, ge=0.001, le=0.1)


class RecordIn(OpIn):
    record: dict


class ScoutIn(BaseModel):
    severity: float | None = Field(None, ge=0, le=1)


def _svc(request: Request) -> SessionService:
    return request.app.state.sessions


@router.get("")
def state(fid: str, request: Request) -> dict:
    return _svc(request).state(fid)


@router.post("/step")
def step(fid: str, request: Request) -> dict:
    return _svc(request).act(fid, "step")


@router.post("/run")
def run(fid: str, body: RunIn, request: Request) -> dict:
    request.app.state.farms.get(fid)
    return request.app.state.jobs.submit("session_run", fid, body.model_dump(), target="local")


@router.post("/approve")
def approve(fid: str, body: OpIn, request: Request) -> dict:
    return _svc(request).act(fid, "approve", {"op_id": body.op_id})


@router.post("/reject")
def reject(fid: str, body: RejectIn, request: Request) -> dict:
    return _svc(request).act(fid, "reject", {"op_id": body.op_id, "reason": body.reason})


@router.post("/defer")
def defer(fid: str, body: DeferIn, request: Request) -> dict:
    return _svc(request).act(fid, "defer", {"op_id": body.op_id, "days": body.days})


@router.post("/fly")
def fly(fid: str, body: FlyIn, request: Request) -> dict:
    return _svc(request).act(fid, "fly_now", {"op_id": body.op_id, "settings": body.settings})


@router.post("/scan")
def scan(fid: str, body: ScanIn, request: Request) -> dict:
    return _svc(request).act(fid, "scan_now", {"gsd_m": body.gsd_m})


@router.post("/upload-scan")
def upload_scan(fid: str, body: UploadIn, request: Request) -> dict:
    return _svc(request).upload_scan(fid, body.geojson, body.gsd_m)


@router.post("/record")
def record(fid: str, body: RecordIn, request: Request) -> dict:
    return _svc(request).record_flight(fid, body.op_id, body.record)


@router.post("/scouting")
def scouting(fid: str, body: ScoutIn, request: Request) -> dict:
    return _svc(request).act(fid, "report_scouting", {"severity": body.severity})


@router.post("/reset")
def reset(fid: str, request: Request) -> dict:
    return _svc(request).reset(fid)


@router.post("/finish")
def finish(fid: str, request: Request) -> dict:
    return _svc(request).finish(fid)


@router.post("/refresh")
def refresh(fid: str, request: Request) -> dict:
    return _svc(request).refresh_weather(fid)


@router.get("/trace")
def trace(fid: str, request: Request) -> dict:
    return _svc(request).trace(fid)


@router.get("/snapshots")
def snapshots(fid: str, request: Request, day: str | None = None) -> dict:
    return _svc(request).snapshots(fid, day)


@router.get("/tracks/{op_id}")
def track(fid: str, op_id: str, request: Request) -> dict:
    return _svc(request).track(fid, op_id)
