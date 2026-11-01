"""Drone twin: spec sheet and missions on a farm."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from fa_drone.mission import MissionSettings

from ..dronesvc import drone_spec_payload, mission_job_factory, simulate

router = APIRouter(prefix="/api", tags=["drone"])
SYNC_MAX_HA = 60.0


def init(app) -> None:
    app.state.jobs.register("mission", mission_job_factory(app.state), remote=True)


class FlightIn(BaseModel):
    height_m: float = Field(2.5, gt=0.5, le=10)
    speed_ms: float = Field(5.0, gt=0.0, le=10)
    swath_m: float = Field(4.0, ge=1.0, le=10)
    rate_l_ha: float = Field(15.0, ge=1, le=100)
    rtk: bool = False
    pump_lpm: float = Field(3.5, gt=0, le=10)
    route_angle_deg: float | None = Field(None, ge=0, le=180)
    ferry_speed_ms: float = Field(8.0, ge=1, le=10)
    n_battery_sets: int = Field(4, ge=1, le=12)
    chargers: int = Field(1, ge=1, le=6)
    refill_s: float = Field(60.0, ge=10, le=900)
    battery_swap_s: float = Field(60.0, ge=10, le=900)
    battery_cycles: int = Field(0, ge=0, le=3000)


class WeatherIn(BaseModel):
    wind_ms: float = Field(2.0, ge=0, le=40)
    wind_dir_deg: float = Field(270.0, ge=0, le=360)
    gust_ms: float = Field(3.2, ge=0, le=60)
    temp_c: float = Field(20.0, ge=-40, le=60)


class MissionIn(BaseModel):
    settings: FlightIn = FlightIn()
    weather: WeatherIn = WeatherIn()
    targets: list[int] | None = Field(None, max_length=200_000)
    home: list[float] | None = Field(None, min_length=2, max_length=2)
    seed: int = Field(0, ge=0, le=1_000_000)


@router.get("/drone/spec")
def spec() -> dict:
    return drone_spec_payload()


@router.post("/farms/{fid}/mission")
def mission(fid: str, body: MissionIn, request: Request) -> dict:
    s = request.app.state
    farm = s.farms.get(fid)
    vf = s.farms.load(fid)
    if (farm["area_ha"] or 0) > SYNC_MAX_HA:
        return {"job": s.jobs.submit("mission", fid, body.model_dump(), target="auto")}
    out = simulate(vf, MissionSettings(**body.settings.model_dump()), body.weather.model_dump(), body.targets,
                   body.seed, farm["region"], tuple(body.home) if body.home else None)
    s.store.add_event(fid, "mission_simulated", {"feasible": out["feasible"],
                                                 **{k: out["kpis"].get(k) for k in ("time_min", "litres", "sorties")}})
    return out
