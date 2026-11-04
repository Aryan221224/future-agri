"""Flying the real drone: the K3-A Pro's on-board agent and ArduPilot flight controllers."""
from __future__ import annotations

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field

from ..aircraft import AgentHub, ArdupilotService

router = APIRouter(prefix="/api", tags=["aircraft"])


def init(app) -> None:
    s = app.state
    s.agent_hub = AgentHub(s.store, s.farms, s.sessions, s.live_run)
    s.ardupilot = ArdupilotService(s.store, s.farms, s.sessions, s.live_run)


class Home(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class AgentCmdIn(BaseModel):
    kind: str = Field(..., max_length=32)
    op_id: str | None = Field(None, max_length=80)
    home: Home | None = None


class ConnectIn(BaseModel):
    link: str = Field(..., max_length=120, pattern=r"^(serial|udpin|udpout|tcp|tcpin|udp):[A-Za-z0-9_./\\:\-\[\]]+$")


class StartIn(BaseModel):
    op_id: str = Field(..., max_length=80)
    home: Home | None = None


# ---------------------------------------------------------------- the agent (device token)
@router.get("/agent/poll")
def agent_poll(request: Request, wait: float = 20.0, x_device_token: str = Header("")) -> dict:
    return request.app.state.agent_hub.poll(x_device_token, wait)


@router.post("/agent/state")
def agent_state(body: dict, request: Request, x_device_token: str = Header("")) -> dict:
    return request.app.state.agent_hub.post_state(x_device_token, body)


@router.post("/agent/ack")
def agent_ack(body: dict, request: Request, x_device_token: str = Header("")) -> dict:
    return request.app.state.agent_hub.ack(x_device_token, body)


# ---------------------------------------------------------------- the operator: K3-A Pro agent
@router.get("/farms/{fid}/aircraft/agent")
def agent_view(fid: str, request: Request) -> dict:
    return request.app.state.agent_hub.view(fid)


@router.post("/farms/{fid}/aircraft/agent/command")
def agent_command(fid: str, body: AgentCmdIn, request: Request) -> dict:
    return request.app.state.agent_hub.command(fid, body.kind, body.op_id,
                                               body.home.model_dump() if body.home else None)


@router.get("/farms/{fid}/aircraft/job-preview")
def job_preview(fid: str, op_id: str, request: Request) -> dict:
    from ..aircraft_jobs import build_job
    s = request.app.state
    return build_job(s.farms, s.sessions, fid, op_id)


# ---------------------------------------------------------------- the operator: ArduPilot
@router.post("/farms/{fid}/aircraft/ardupilot/connect")
def ap_connect(fid: str, body: ConnectIn, request: Request) -> dict:
    return request.app.state.ardupilot.connect(fid, body.link)


@router.post("/farms/{fid}/aircraft/ardupilot/disconnect")
def ap_disconnect(fid: str, request: Request) -> dict:
    return request.app.state.ardupilot.disconnect(fid)


@router.get("/farms/{fid}/aircraft/ardupilot")
def ap_status(fid: str, request: Request) -> dict:
    return request.app.state.ardupilot.status(fid)


@router.get("/farms/{fid}/aircraft/ardupilot/preflight")
def ap_preflight(fid: str, request: Request) -> dict:
    return request.app.state.ardupilot.preflight(fid)


@router.post("/farms/{fid}/aircraft/ardupilot/start")
def ap_start(fid: str, body: StartIn, request: Request) -> dict:
    return request.app.state.ardupilot.start(fid, body.op_id, body.home.model_dump() if body.home else None)


@router.post("/farms/{fid}/aircraft/ardupilot/battery-swapped")
def ap_swapped(fid: str, request: Request) -> dict:
    return request.app.state.ardupilot.battery_swapped(fid)


@router.post("/farms/{fid}/aircraft/ardupilot/abort")
def ap_abort(fid: str, request: Request) -> dict:
    return request.app.state.ardupilot.abort(fid)
