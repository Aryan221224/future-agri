"""Advisory endpoints: the field twin over HTTP.

Separate from the database-backed endpoints in ``main.py`` so the twin can be
used (and tested) with no database. Twins live in memory per process; that is
fine for a pilot with a handful of fields and is the obvious thing to move into
the store once the loop is validated in the field.

Forecasts: in production the weekly plan uses a real forecast ensemble
(Open-Meteo ensemble API). Until that feed is connected, the endpoints accept
forecast members in the request, and otherwise fall back to *climatology*
members -- and say so in the response, because a plan made without a forecast
is a different (weaker) thing.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/twin", tags=["twin"])
_TWINS: dict[str, Any] = {}


class FieldSetup(BaseModel):
    crop: str = "potato"
    variety: str = "Potato_701"
    latitude: float = 28.45
    longitude: float = 77.58
    sowing_date: dt.date
    harvest_date: dt.date
    smfcf: float = 0.32
    smw: float = 0.15
    inoculum: bool | None = None
    weather: list[dict] = Field(default_factory=list,
                                description="observed daily records: DAY, TMAX, TMIN, RAIN(mm), IRRAD(J/m2), VAP(hPa), WIND(m/s)")
    synthetic_weather_seed: int | None = Field(
        None, description="use the normals-calibrated generator instead of observed records (demo only)")


class PassIn(BaseModel):
    day: dt.date
    canopy_cover: float = Field(ge=0.0, le=1.0)
    growth_stage_dvs: float | None = None
    weed_density_cells: list[float] | None = None


class AdviceIn(BaseModel):
    day: dt.date
    forecast: list[list[dict]] | None = None
    seed: int = 0


def _parse(records: list[dict]) -> list[dict]:
    out = []
    for r in records:
        r = dict(r)
        if isinstance(r["DAY"], str):
            r["DAY"] = dt.date.fromisoformat(r["DAY"])
        out.append(r)
    return out


def _twin(field_id: str):
    tw = _TWINS.get(field_id)
    if tw is None:
        raise HTTPException(404, f"no twin for field {field_id!r}; POST /twin/{field_id}/setup first")
    return tw


@router.post("/{field_id}/setup")
def setup(field_id: str, body: FieldSetup) -> dict:
    from fa_loop.orchestrator import FieldTwin
    from fa_twin.engine import SoilParams, ZoneConfig, check_photoperiod
    from fa_twin.weather import generate_records

    cfg = ZoneConfig(zone_id=field_id, latitude=body.latitude, longitude=body.longitude,
                     crop=body.crop, variety=body.variety, sowing_date=body.sowing_date,
                     harvest_date=body.harvest_date, soil=SoilParams(SMFCF=body.smfcf, SMW=body.smw))
    gate = check_photoperiod(cfg)
    if not gate.ok:
        raise HTTPException(422, {"refused": True, "reasons": gate.reasons})
    if body.weather:
        recs, source = _parse(body.weather), "observed"
    elif body.synthetic_weather_seed is not None:
        start = body.sowing_date - dt.timedelta(days=24)
        days = (body.harvest_date - start).days + 30
        recs, source = generate_records(start, days, body.synthetic_weather_seed), \
            "SYNTHETIC normals-calibrated weather (demo, not for advice)"
    else:
        raise HTTPException(422, "provide observed weather records or synthetic_weather_seed")
    _TWINS[field_id] = FieldTwin(cfg, recs, inoculum=body.inoculum)
    return {"field_id": field_id, "feasible": True, "warnings": gate.reasons,
            "weather_source": source}


@router.post("/{field_id}/pass")
def camera_pass(field_id: str, body: PassIn) -> dict:
    tw = _twin(field_id)
    cells = np.asarray(body.weed_density_cells) if body.weed_density_cells else None
    d = tw.ingest_pass(body.day, body.canopy_cover, body.growth_stage_dvs, cells)
    return {"day": str(d.day), "detail": d.detail, **{k: v for k, v in d.data.items()}}


@router.post("/{field_id}/advice")
def advice(field_id: str, body: AdviceIn) -> dict:
    from fa_twin.weather import forecast_ensemble

    tw = _twin(field_id)
    note = "forecast supplied"
    fc = [_parse(m) for m in body.forecast] if body.forecast else None
    if fc is None:
        fc = forecast_ensemble(tw.records, body.day, tw.s.plan_members, seed=body.seed,
                               forecast_days=0)
        note = "no forecast feed connected: planned on climatology members only"
    irr = tw.irrigation_advice(body.day, fc)
    weekly = tw.weekly_plan(body.day, fc, seed=body.seed)
    fd = weekly["fungicide"]
    return {
        "day": str(body.day), "forecast": note,
        "irrigate": {"action": irr.action, "reason": irr.detail, **irr.data},
        "fungicide": {"spray_day": str(fd.spray_day) if fd.spray_day else None,
                      "expected_gain_rs_ha": round(fd.expected_gain),
                      "p_spray_better": round(fd.p_spray_better, 2), "reason": fd.reason},
        "scout": weekly.get("scout", False),
        "state": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in tw.state().items()},
    }


@router.post("/{field_id}/irrigated")
def irrigated(field_id: str, day: dt.date, amount_cm: float) -> dict:
    tw = _twin(field_id)
    tw.record_irrigation(day, amount_cm)
    return {"water_cm_total": tw.water_cm}


@router.post("/{field_id}/sprayed")
def sprayed(field_id: str, day: dt.date) -> dict:
    tw = _twin(field_id)
    tw.record_spray(day)
    return {"sprays": [str(d) for d in tw.sprays], "protected_until": str(tw.protected_until)}


@router.get("/{field_id}/log")
def decision_log(field_id: str) -> list[dict]:
    return _twin(field_id).advice_log()


@router.get("/simulate/season")
def simulate(seed: int = 0, policy: str = "twin", members: int = 10) -> dict:
    """One closed-loop season against a simulated truth (demo and teaching)."""
    from fa_loop.season import POLICIES, simulate_season
    if policy not in POLICIES:
        raise HTTPException(422, f"policy must be one of {POLICIES}")
    r = simulate_season(seed, policy, plan_members=max(10, min(members, 20)), keep_log=True)
    out = {k: v for k, v in r.__dict__.items() if k != "log"}
    out["log"] = [{k: (str(v) if isinstance(v, (dt.date, np.floating)) else v) for k, v in e.items()}
                  for e in r.log]
    out["note"] = "SIMULATED season; blight, weed and price figures are stated assumptions"
    return out


# ---------------------------------------------------------------- review queue
#
# Detections the action gate could not settle (crop or weed?) come here. A
# person labels them; the labels are exactly what the field recalibration needs
# (``fa_core.uncertainty.recalibrate_with_field_labels``: 100-200 field labels
# restore coverage that a German-maize calibration cannot promise in an Indian
# potato field).

_REVIEW: dict[str, list[dict]] = {}
REVIEW_LABELS = ("crop", "weed", "background")


class ReviewItemIn(BaseModel):
    frame_id: str
    bbox: list[float]
    label_set: list[str]
    top_class: str
    reason: str
    probs: list[float] | None = None
    thumbnail: str | None = Field(None, description="data: URL or path of the crop thumbnail")


@router.post("/{field_id}/review")
def add_review_items(field_id: str, items: list[ReviewItemIn]) -> dict:
    q = _REVIEW.setdefault(field_id, [])
    for it in items:
        q.append({"id": len(q), **it.model_dump(), "status": "pending", "label": None})
    return {"queued": len(items), "pending": sum(i["status"] == "pending" for i in q)}


@router.get("/{field_id}/review")
def list_review(field_id: str, status: str = "pending") -> list[dict]:
    return [i for i in _REVIEW.get(field_id, []) if status == "all" or i["status"] == status]


@router.post("/{field_id}/review/{item_id}")
def label_review(field_id: str, item_id: int, label: str) -> dict:
    if label not in REVIEW_LABELS:
        raise HTTPException(422, f"label must be one of {REVIEW_LABELS}")
    q = _REVIEW.get(field_id, [])
    if not 0 <= item_id < len(q):
        raise HTTPException(404, "no such review item")
    q[item_id].update(status="labelled", label=label)
    n = sum(i["status"] == "labelled" for i in q)
    return {"id": item_id, "label": label, "field_labels": n,
            "recalibration": ("possible: enough field labels" if n >= 100
                              else f"needs {100 - n} more field labels")}
