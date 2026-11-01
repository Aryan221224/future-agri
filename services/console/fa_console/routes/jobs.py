"""Jobs: list, inspect, stream progress (SSE), cancel."""
from __future__ import annotations

import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ..errors import NotFound

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
def list_jobs(request: Request, farm_id: str | None = None, kind: str | None = None,
              limit: int = Query(50, ge=1, le=500)) -> dict:
    return {"jobs": request.app.state.store.list_jobs(farm_id, limit, kind)}


@router.get("/{jid}")
def get_job(jid: str, request: Request) -> dict:
    j = request.app.state.store.get_job(jid)
    if j is None:
        raise NotFound(f"no job {jid}")
    return j


@router.get("/{jid}/result")
def job_result(jid: str, request: Request) -> dict:
    r = request.app.state.jobs.result(jid)
    if r is None:
        raise NotFound(f"job {jid} has no result")
    return r


@router.get("/{jid}/events")
async def job_events(jid: str, request: Request):
    jm = request.app.state.jobs
    if request.app.state.store.get_job(jid) is None:
        raise NotFound(f"no job {jid}")

    async def gen():
        async for ev in jm.events(jid):
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{jid}/cancel")
def cancel(jid: str, request: Request) -> dict:
    return request.app.state.jobs.cancel(jid)
