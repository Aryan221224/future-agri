"""Live run: a spray job flown as a closed loop (simulated camera or the aircraft's messages)."""
from __future__ import annotations

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field

from ..live_run import LiveRunService

router = APIRouter(prefix="/api", tags=["live run"])


def init(app) -> None:
    s = app.state
    s.live_run = LiveRunService(s.store, s.farms, s.sessions, s.jobs, s.exports)


class StartIn(BaseModel):
    op_id: str = Field(..., max_length=80)
    valve: bool = False
    source: str | None = Field(None, pattern="^(simulated|aircraft)$")


@router.post("/farms/{fid}/live-run")
def start(fid: str, body: StartIn, request: Request) -> dict:
    return request.app.state.live_run.start(fid, body.op_id, body.valve, body.source)


@router.get("/farms/{fid}/live-run")
def state(fid: str, request: Request) -> dict:
    return request.app.state.live_run.state(fid)


@router.post("/farms/{fid}/live-run/freeze")
def freeze(fid: str, request: Request) -> dict:
    return request.app.state.live_run.freeze(fid)


@router.post("/farms/{fid}/live-run/finish")
def finish(fid: str, request: Request) -> dict:
    return request.app.state.live_run.finish(fid)


@router.get("/farms/{fid}/live-run/next-sortie.kml")
def next_sortie(fid: str, request: Request) -> Response:
    kml = request.app.state.live_run.next_sortie_kml(fid)
    return Response(kml, media_type="application/vnd.google-earth.kml+xml",
                    headers={"Content-Disposition": f'attachment; filename="next-sortie-{fid}.kml"'})


@router.post("/live-run/ingest")
def ingest(batch: dict, request: Request, x_device_token: str | None = Header(None)) -> dict:
    """From the on-board agent: one message batch (schema fa.live/1), device-token authenticated."""
    if not x_device_token:
        raise PermissionError("X-Device-Token header required")
    return request.app.state.live_run.ingest(x_device_token, batch)
