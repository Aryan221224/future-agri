"""Iterations and plans."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..errors import Busy, NotFound
from ..plans import make_plan, perfect_job_factory

router = APIRouter(prefix="/api", tags=["plans"])


def init(app) -> None:
    app.state.jobs.register("perfect", perfect_job_factory(app.state), remote=True)


class PerfectIn(BaseModel):
    seeds: int = Field(3, ge=1, le=10)
    members: int = Field(6, ge=2, le=30)
    quick: bool = False
    workers: int | None = Field(None, ge=1, le=256)
    target: Literal["auto", "local", "remote"] = "auto"


class PlanIn(BaseModel):
    job_id: str
    plan: str | None = None


class ApproveIn(BaseModel):
    confirm_reset: bool = False


@router.post("/farms/{fid}/perfect")
def perfect(fid: str, body: PerfectIn, request: Request) -> dict:
    s = request.app.state
    f = s.farms.get(fid)
    if f["status"] != "ready":
        raise ValueError("the farm is not built yet")
    params = body.model_dump(exclude={"target"})
    return s.jobs.submit("perfect", fid, params, target=body.target)


@router.get("/farms/{fid}/iterations")
def iterations(fid: str, request: Request) -> dict:
    request.app.state.farms.get(fid)
    return {"jobs": request.app.state.store.list_jobs(fid, 50, kind="perfect")}


@router.get("/farms/{fid}/plans")
def plans(fid: str, request: Request) -> dict:
    request.app.state.farms.get(fid)
    return {"plans": request.app.state.store.list_plans(fid)}


@router.post("/farms/{fid}/plans")
def create_plan(fid: str, body: PlanIn, request: Request) -> dict:
    s = request.app.state
    farm = s.farms.get(fid)
    job = s.store.get_job(body.job_id)
    if job is None or job["farm_id"] != fid or job["kind"] != "perfect":
        raise NotFound("no iterations job with that id for this farm")
    if job["status"] != "done":
        raise ValueError(f"the iterations job is not finished ({job['status']})")
    res = s.jobs.result(job["id"])
    payload = make_plan(farm, res, body.plan)
    plan = s.store.add_plan(fid, payload, source_job=job["id"])
    s.store.add_event(fid, "plan_drafted", {"plan": plan["id"], "name": payload["plan"]})
    return plan


@router.get("/plans/{pid}")
def get_plan(pid: str, request: Request) -> dict:
    p = request.app.state.store.get_plan(pid)
    if p is None:
        raise NotFound(f"no plan {pid}")
    return p


@router.post("/plans/{pid}/approve")
def approve(pid: str, body: ApproveIn, request: Request) -> dict:
    s = request.app.state
    plan = get_plan(pid, request)
    fid = plan["farm_id"]
    st = s.sessions.state(fid)
    progressed = st["n_events"] > 0 or st["das"] > 1
    if progressed and not body.confirm_reset:
        raise Busy(f"approving this plan resets the mission session (now on {st['day']}); confirm to proceed")
    for other in s.store.list_plans(fid):
        if other["status"] == "approved" and other["id"] != pid:
            s.store.set_plan_status(other["id"], "superseded")
    s.store.set_plan_status(pid, "approved")
    s.store.add_event(fid, "plan_approved", {"plan": pid, "name": plan["payload"]["plan"]})
    s.sessions.reset(fid)
    return s.store.get_plan(pid)
