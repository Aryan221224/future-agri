"""Remote workers (GPU server over an SSH reverse tunnel): claim, progress, complete."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..detect import console_ortho_path
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
    s = request.app.state
    job = s.jobs.claim(body.token, body.worker, body.kinds, body.info)
    if job is None:
        return Response(status_code=204)
    base = f"/api/workers/artefact/{job['id']}"
    job["artefact"] = base if job["farm_id"] else None
    job["files"] = {}
    if job["farm_id"]:
        job["farm"] = s.store.get_farm(job["farm_id"])
        job["files"]["farm"] = f"{base}?name=farm"
    if job["params"].get("ortho"):
        job["files"]["ortho"] = f"{base}?name=ortho"
    job["console"] = {"offline": s.settings.offline}
    return job


@router.post("/{jid}/progress")
def progress(jid: str, body: Progress, request: Request) -> dict:
    return request.app.state.jobs.report(body.token, jid, body.progress, body.message)


@router.post("/{jid}/complete")
def complete(jid: str, body: Complete, request: Request) -> dict:
    return request.app.state.jobs.complete(body.token, jid, body.result, body.error)


@router.get("/artefact/{jid}")
def artefact(jid: str, request: Request, token: str | None = None, name: str = "farm"):
    """A job's input files for a remote worker: the built farm, or an uploaded orthomosaic.
    The worker token comes in the ``X-Worker-Token`` header (or ``token`` for old workers)."""
    jm, st = request.app.state.jobs, request.app.state.store
    jm._auth(request.headers.get("x-worker-token") or token or "")
    job = st.get_job(jid)
    if job is None or not job["farm_id"]:
        raise NotFound(f"job {jid} has no farm artefact")
    if name == "farm":
        p = st.farm_dir(job["farm_id"]) / "farm.pkl"
        if not p.exists():
            raise NotFound("farm not built")
        return FileResponse(p, media_type="application/octet-stream", filename="farm.pkl")
    if name == "ortho" and job["params"].get("ortho"):
        p = console_ortho_path(st, job)
        if not p.exists():
            raise NotFound("orthomosaic no longer on the console")
        return FileResponse(p, media_type="image/tiff", filename=p.name)
    raise NotFound(f"job {jid} has no '{name}' file")

