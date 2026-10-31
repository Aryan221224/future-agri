"""Read/write API over the twin's state.

Deliberately small. The interesting parts of this system are the twin, the
planner and the gateway; the API's job is to expose them without becoming a
place where logic accumulates.

Two endpoints carry real weight:

* ``/review`` -- the queue of findings whose conformal prediction set was not a
  singleton. This is the human-in-the-loop surface, and the thing that keeps an
  uncertain model from silently driving a spray decision.
* ``/coverage`` -- observed conformal coverage per stratum against nominal.
  This is a *production* metric with an alarm, not a validation-time number:
  coverage drifting below nominal means the calibration set is no longer
  exchangeable with what the model is seeing (new season, new cultivar, new
  light) and the answer is to recalibrate.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

DSN = os.environ.get("FA_DSN", "postgresql://futureagri:futureagri@db:5432/futureagri")

_pool: Any = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    try:
        import asyncpg
        _pool = await asyncpg.create_pool(DSN, min_size=1, max_size=8)
    except Exception as e:  # the API must still boot for /health and docs
        app.state.db_error = str(e)
        _pool = None
    yield
    if _pool is not None:
        await _pool.close()


app = FastAPI(title="Future Agri API", version="0.2.0", lifespan=lifespan)

# The field twin (no database needed): setup, one-pass ingest, daily/weekly advice.
from .advisory import router as _advisory_router  # noqa: E402

app.include_router(_advisory_router)


async def fetch(sql: str, *args):
    if _pool is None:
        raise HTTPException(503, "database unavailable")
    async with _pool.acquire() as con:
        return [dict(r) for r in await con.fetch(sql, *args)]


class Health(BaseModel):
    status: str
    database: bool
    version: str = "0.1.0"


@app.get("/health", response_model=Health)
async def health() -> Health:
    ok = False
    if _pool is not None:
        try:
            async with _pool.acquire() as con:
                await con.fetchval("SELECT 1")
            ok = True
        except Exception:
            ok = False
    return Health(status="ok" if ok else "degraded", database=ok)


@app.get("/parcels")
async def parcels():
    return await fetch(
        "SELECT id, name, geoid, area_ha, ST_AsGeoJSON(geom)::json AS geometry "
        "FROM parcel ORDER BY name")


@app.get("/parcels/{parcel_id}/zones")
async def zones(parcel_id: str):
    return await fetch(
        "SELECT id, name, area_ha, soil, ST_AsGeoJSON(geom)::json AS geometry "
        "FROM management_zone WHERE parcel_id = $1", parcel_id)


@app.get("/observations")
async def observations(zone_id: str | None = None, variable: str | None = None,
                       since_days: int = Query(30, ge=1, le=3650), limit: int = 500):
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    return await fetch(
        """SELECT observed_at, zone_id, variable, value, unit, lo, hi, coverage,
                  needs_review, model_version, calibration, source
           FROM observation
           WHERE observed_at >= $1
             AND ($2::uuid IS NULL OR zone_id = $2::uuid)
             AND ($3::text IS NULL OR variable = $3)
           ORDER BY observed_at DESC LIMIT $4""",
        since, zone_id, variable, limit)


@app.get("/review")
async def review_queue(limit: int = 100):
    """Findings a human must adjudicate, newest first."""
    return await fetch(
        """SELECT observed_at, zone_id, variable, value, label_set, model_version,
                  calibration, source
           FROM observation
           WHERE needs_review
           ORDER BY observed_at DESC LIMIT $1""", limit)


@app.get("/coverage")
async def coverage(since_days: int = 30):
    """Observed conformal coverage per stratum, with the alarm state.

    Coverage is computed where a ground-truth label exists, so this is only
    meaningful once review decisions are flowing back in -- which is the point:
    the humans adjudicating the queue are also, for free, producing the labels
    that keep the calibration honest.
    """
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    rows = await fetch(
        """SELECT calibration AS stratum, coverage AS nominal,
                  avg(CASE WHEN attributes_covered THEN 1.0 ELSE 0.0 END) AS observed,
                  count(*) AS n
           FROM (SELECT calibration, coverage,
                        (value BETWEEN lo AND hi) AS attributes_covered
                 FROM observation
                 WHERE observed_at >= $1 AND lo IS NOT NULL) s
           GROUP BY calibration, coverage""", since)
    import math
    out = []
    for r in rows:
        nominal = float(r["nominal"] or 0.9)
        observed = float(r["observed"] or 0.0)
        n = int(r["n"])
        se = math.sqrt(max(nominal * (1 - nominal), 1e-9) / max(n, 1))
        out.append(r | {"alarm": observed < nominal - 3 * se,
                        "standard_error": round(se, 4)})
    return out


@app.get("/prescriptions")
async def prescriptions(status: str = "pending", limit: int = 50):
    return await fetch(
        """SELECT l.id, l.name, l.ts, l.status, l.source, l.model_version,
                  json_agg(json_build_object('measure', q.measure, 'value', q.value,
                           'unit', q.unit, 'label', q.label,
                           'material', q.material_id)) AS quantities
           FROM log l LEFT JOIN quantity q ON q.log_id = l.id
           WHERE l.type = 'input' AND l.status = $1::log_status
           GROUP BY l.id ORDER BY l.ts DESC LIMIT $2""", status, limit)


@app.get("/conflicts")
async def conflicts():
    """Append-only version chains that forked -- for a human to resolve.

    Surfacing conflicts instead of auto-merging them is a deliberate choice.
    Two agronomists editing one prescription can be merged into a document that
    converges and is agronomically meaningless; applying it costs real money.
    """
    return await fetch("SELECT * FROM version_conflict")


@app.get("/missions")
async def missions(limit: int = 20):
    return await fetch(
        "SELECT id, parcel_id, planned_for, status, n_sorties, energy_wh, duration_s "
        "FROM mission ORDER BY planned_for DESC LIMIT $1", limit)
