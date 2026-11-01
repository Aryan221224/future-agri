"""Remote workers (GPU server over an SSH reverse tunnel): claim, progress, complete."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..errors import NotFound

router = APIRouter(prefix="/api/workers", tags=["workers"])


class Claim(BaseModel):
    token: str
    worker: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    kinds: list[str]
    info: dict = {}


class Progress(BaseModel):
    token: str
    progress: float = Field(..., ge=0, le=1)
    message: str = ""


class Complete(BaseModel):
    token: str
    result: dict | None = None
    error: str | None = None


@router.get("")
def workers(request: Request) -> dict:
    return request.app.state.jobs.workers()


@router.get("/token")
def token(request: Request) -> dict:
    s = request.app.state.settings
    return {"token": request.app.state.jobs.worker_token(), "port": s.port,
            "tunnel_port": 18099, "kinds": sorted(request.app.state.jobs._remote_kinds)}


@router.post("/token/rotate")
def rotate(request: Request) -> dict:
    request.app.state.store.add_event(None, "worker_token_rotated", {})
    return {"token": request.app.state.jobs.rotate_worker_token()}


@router.post("/claim")
def claim(body: Claim, request: Request):
    job = request.app.state.jobs.claim(body.token, body.worker, body.kinds, body.info)
    if job is None:
        return Response(status_code=204)
    job["artefact"] = f"/api/workers/artefact/{job['id']}" if job["farm_id"] else None
    return job


@router.post("/{jid}/progress")
def progress(jid: str, body: Progress, request: Request) -> dict:
    return request.app.state.jobs.report(body.token, jid, body.progress, body.message)


@router.post("/{jid}/complete")
def complete(jid: str, body: Complete, request: Request) -> dict:
    return request.app.state.jobs.complete(body.token, jid, body.result, body.error)


@router.get("/artefact/{jid}")
def artefact(jid: str, token: str, request: Request):
    jm, st = request.app.state.jobs, request.app.state.store
    jm._auth(token)
    job = st.get_job(jid)
    if job is None or not job["farm_id"]:
        raise NotFound(f"job {jid} has no farm artefact")
    p = st.farm_dir(job["farm_id"]) / "farm.pkl"
    if not p.exists():
        raise NotFound("farm not built")
    return FileResponse(p, media_type="application/octet-stream", filename="farm.pkl")
