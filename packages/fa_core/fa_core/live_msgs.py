"""Live message schema ``fa.live/1``: what the on-board agent on the sprayer sends.

A batch carries telemetry (where the aircraft is, whether it sprays) and per-cell summaries
of what the camera saw (raw weed detections over the area seen, at a GSD) -- tens of bytes
per cell, never imagery. ``seq`` makes store-and-forward replays safe: the console counts a
(run_id, seq) once. Items are checked one by one; a bad item is reported and dropped, the
rest of the batch is kept.

    {"schema": "fa.live/1", "run_id": "run_...", "seq": 0, "sent_at": "<ISO-8601>",
     "telemetry": [{"t", "lat", "lon", "alt_agl_m", "heading_deg", "speed_ms",
                    "spraying": bool, "sortie": int >= 1, "flow_lpm"?, "tank_l"?, "soc"?}],
     "cells": [{"t", "cell": int, "weeds": int >= 0, "area_m2": > 0, "gsd_m": > 0,
                "blight": bool, "cc"?: 0..1}]}
"""
from __future__ import annotations

import math

SCHEMA = "fa.live/1"
MAX_ITEMS = 20_000


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _telemetry_error(p: dict) -> str | None:
    if not isinstance(p, dict):
        return "not an object"
    for k in ("lat", "lon", "alt_agl_m", "heading_deg", "speed_ms"):
        if not _num(p.get(k)):
            return f"{k} is not a number"
    if not -90 <= p["lat"] <= 90 or not -180 <= p["lon"] <= 180:
        return "lat/lon out of range"
    if not -1 <= p["alt_agl_m"] <= 200:
        return "alt_agl_m out of range (-1..200 m)"
    if not 0 <= p["speed_ms"] <= 60:
        return "speed_ms out of range (0..60 m/s)"
    if not isinstance(p.get("spraying"), bool):
        return "spraying is not true/false"
    if not _int(p.get("sortie")) or p["sortie"] < 1:
        return "sortie is not a whole number >= 1"
    if not isinstance(p.get("t"), (str, int, float)):
        return "t is missing"
    for k in ("flow_lpm", "tank_l", "soc"):
        if k in p and p[k] is not None and not _num(p[k]):
            return f"{k} is not a number"
    return None


def _cell_error(c: dict, n_cells: int) -> str | None:
    if not isinstance(c, dict):
        return "not an object"
    if not _int(c.get("cell")) or not 0 <= c["cell"] < n_cells:
        return f"cell index outside the field grid (0..{n_cells - 1})"
    if not _int(c.get("weeds")) or c["weeds"] < 0:
        return "weeds is not a whole number >= 0"
    if not _num(c.get("area_m2")) or c["area_m2"] <= 0:
        return "area_m2 is not a positive number"
    if not _num(c.get("gsd_m")) or not 0 < c["gsd_m"] <= 1:
        return "gsd_m is not in (0, 1] m"
    if not isinstance(c.get("blight", False), bool):
        return "blight is not true/false"
    if "cc" in c and c["cc"] is not None and (not _num(c["cc"]) or not 0 <= c["cc"] <= 1):
        return "cc is not in 0..1"
    return None


def validate_batch(batch: dict, n_cells: int, polygon_wgs84=None) -> tuple[dict, list[str]]:
    """(batch with only the valid items, errors per dropped item). A bad envelope raises."""
    if not isinstance(batch, dict) or batch.get("schema") != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA}")
    if not isinstance(batch.get("run_id"), str) or not batch["run_id"] or len(batch["run_id"]) > 80:
        raise ValueError("run_id must be a non-empty string")
    if not _int(batch.get("seq")) or batch["seq"] < 0:
        raise ValueError("seq must be a whole number >= 0")
    tel, cells = batch.get("telemetry", []), batch.get("cells", [])
    if not isinstance(tel, list) or not isinstance(cells, list):
        raise ValueError("telemetry and cells must be lists")
    if len(tel) + len(cells) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items per batch")
    errs: list[str] = []
    ok_t, ok_c = [], []
    for i, p in enumerate(tel):
        e = _telemetry_error(p)
        (errs.append(f"telemetry {i}: {e}") if e else ok_t.append(p))
    for i, c in enumerate(cells):
        e = _cell_error(c, n_cells)
        (errs.append(f"cell {i}: {e}") if e else ok_c.append(c))
    return {**batch, "telemetry": ok_t, "cells": ok_c}, errs
