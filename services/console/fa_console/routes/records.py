"""Spray records, exports and the farmer's Today card."""
from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response

from ..advice import today as today_card
from ..exports import ExportService
from ..records import RecordService

router = APIRouter(prefix="/api/farms/{fid}", tags=["records"])


def init(app) -> None:
    s = app.state
    s.records = RecordService(s.store, s.settings, s.farms)
    s.exports = ExportService(s.store, s.settings, s.farms, s.sessions)


def _dl(content, media: str, filename: str):
    return Response(content=content, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:40] or "farm"


@router.get("/records")
def records(fid: str, request: Request) -> dict:
    return {"records": request.app.state.records.list(fid)}


@router.post("/records")
def add_record(fid: str, body: dict, request: Request) -> dict:
    return request.app.state.records.add_manual(fid, body)


@router.post("/records/import")
async def import_records(fid: str, request: Request, file: UploadFile = File(...),
                         mapping: str | None = Form(None)) -> dict:
    data = await file.read()
    m = json.loads(mapping) if mapping else None
    if m is not None and not isinstance(m, dict):
        raise ValueError("mapping must be a JSON object")
    return request.app.state.records.import_jiyi(fid, data, file.filename or "upload", m)


@router.get("/export/kml")
def export_kml(fid: str, request: Request, op: str | None = None):
    f = request.app.state.farms.get(fid)
    return _dl(request.app.state.exports.kml(fid, op), "application/vnd.google-earth.kml+xml",
               f"{_safe(f['name'])}{'_' + _safe(op) if op else ''}.kml")


@router.get("/export/survey.kml")
def export_survey(fid: str, request: Request, gsd_m: float = 0.01):
    f = request.app.state.farms.get(fid)
    if not 0.001 <= gsd_m <= 0.1:
        raise ValueError("gsd_m must be 0.001-0.1")
    return _dl(request.app.state.exports.survey_kml(fid, gsd_m), "application/vnd.google-earth.kml+xml",
               f"{_safe(f['name'])}_survey.kml")


@router.get("/export/settings")
def export_settings(fid: str, request: Request) -> dict:
    return request.app.state.exports.sheet(fid)


@router.get("/export/plan.csv")
def export_plan_csv(fid: str, request: Request):
    f = request.app.state.farms.get(fid)
    return _dl(request.app.state.exports.plan_csv(fid), "text/csv", f"{_safe(f['name'])}_plan.csv")


@router.get("/export/plan.ics")
def export_plan_ics(fid: str, request: Request):
    f = request.app.state.farms.get(fid)
    return _dl(request.app.state.exports.plan_ics(fid), "text/calendar", f"{_safe(f['name'])}_plan.ics")


@router.get("/export/plan.pdf")
def export_plan_pdf(fid: str, request: Request):
    f = request.app.state.farms.get(fid)
    return _dl(request.app.state.exports.plan_pdf(fid), "application/pdf", f"{_safe(f['name'])}_plan.pdf")


@router.get("/export/records.csv")
def export_records(fid: str, request: Request):
    s = request.app.state
    f = s.farms.get(fid)
    return _dl(s.exports.records_csv(fid, s.records.list(fid)), "text/csv", f"{_safe(f['name'])}_records.csv")


@router.get("/export/bundle")
def export_bundle(fid: str, request: Request):
    f = request.app.state.farms.get(fid)
    return _dl(request.app.state.exports.bundle_zip(fid), "application/zip", f"{_safe(f['name'])}_bundle.zip")


@router.get("/today")
def today(fid: str, request: Request) -> dict:
    s = request.app.state
    farm = s.farms.get(fid)
    st = s.sessions.state(fid)
    outlook = s.live.outlook(fid)
    card = today_card(st, farm, outlook if outlook.get("available") else None)
    card["alerts"] = s.live.alerts(fid)["alerts"]
    return card
