"""Stock K3-A Pro closed loop (no added hardware) and the Agri Assistant remote over ADB."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ..rc_link import RcLink
from ..stock_loop import StockLoopService

router = APIRouter(prefix="/api", tags=["stock K3-A Pro"])


def init(app) -> None:
    s = app.state
    s.rc = RcLink()
    s.stock_loop = StockLoopService(s.store, s.farms, s.sessions, s.exports, rc=s.rc)


class StartIn(BaseModel):
    op_id: str = Field(..., max_length=80)
    tank_l: float | None = Field(None, ge=0.5, le=100)


class RecordIn(BaseModel):
    litres: float = Field(..., ge=0, le=100_000)
    area_ha: float = Field(..., gt=0, le=10_000)
    flight_min: float | None = Field(None, ge=0, le=600)
    block: int | None = Field(None, ge=1)


class ConnectIn(BaseModel):
    host: str = Field(..., max_length=64, pattern=r"^[A-Za-z0-9.\-:\[\]]+$")


@router.post("/farms/{fid}/stock-loop")
def start(fid: str, body: StartIn, request: Request) -> dict:
    return request.app.state.stock_loop.start(fid, body.op_id, body.tank_l)


@router.get("/farms/{fid}/stock-loop")
def state(fid: str, request: Request) -> dict:
    return request.app.state.stock_loop.state(fid)


@router.get("/farms/{fid}/stock-loop/block.kml")
def block_kml(fid: str, request: Request, n: int | None = None) -> Response:
    kml = request.app.state.stock_loop.block_kml(fid, n)
    return Response(kml, media_type="application/vnd.google-earth.kml+xml",
                    headers={"Content-Disposition": f'attachment; filename="block_{n or "next"}.kml"'})


@router.post("/farms/{fid}/stock-loop/record")
def record(fid: str, body: RecordIn, request: Request) -> dict:
    return request.app.state.stock_loop.record(fid, body.model_dump(exclude={"block"}), body.block)


@router.post("/farms/{fid}/stock-loop/pull")
def pull(fid: str, request: Request) -> dict:
    return request.app.state.stock_loop.pull_records(fid)


@router.post("/farms/{fid}/stock-loop/finish")
def finish(fid: str, request: Request) -> dict:
    return request.app.state.stock_loop.finish(fid)


@router.get("/rc")
def rc_status(request: Request) -> dict:
    return request.app.state.rc.status()


@router.post("/rc/connect")
def rc_connect(body: ConnectIn, request: Request) -> dict:
    request.app.state.rc.connect(body.host)
    return request.app.state.rc.status()


@router.get("/rc/screen.png")
def rc_screen(request: Request) -> Response:
    return Response(request.app.state.rc.screenshot(), media_type="image/png")
