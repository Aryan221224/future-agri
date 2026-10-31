"""The K3-A Pro's only integration points: KML polygons in, job records out.

JIYI Agri Assistant imports one KML file at a time (from the phone's kitJson
folder), polygons only, up to 400 waypoints per group; route parameters (height,
speed, row spacing, dose per area, flow mode) are set in the app. So the twin
exports the field and exclusion polygons plus a settings sheet the operator
enters, and imports the app's job records afterwards. The record format is
undocumented, so import is a tolerant field mapping with explicit validation:
the phone app is an untrusted boundary and nothing is silently dropped.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

MU_M2 = 10_000.0 / 15.0          # 1 mu = 666.67 m2
ACRE_M2 = 4046.8564224
KML_NS = "http://www.opengis.net/kml/2.2"


def _ring_coords(poly) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in poly.exterior.coords]


def _simplify_to_limit(poly_wgs84, limit: int):
    from fa_core.geo import to_local, to_wgs84
    n = len(poly_wgs84.exterior.coords)
    if n <= limit:
        return poly_wgs84, False
    loc, crs = to_local(poly_wgs84)
    tol = 0.05
    simp = loc
    while len(simp.exterior.coords) > limit and tol < 50:
        simp = loc.simplify(tol, preserve_topology=True)
        tol *= 1.5
    return to_wgs84(simp, crs), True


def export_kml(field_wgs84, exclusions_wgs84: list, name: str, path, max_vertices: int = 400,
               report: dict | None = None) -> Path:
    """Write a KML the app can import: one Polygon per field / exclusion area."""
    ET.register_namespace("", KML_NS)
    kml = ET.Element(f"{{{KML_NS}}}kml")
    doc = ET.SubElement(kml, f"{{{KML_NS}}}Document")
    ET.SubElement(doc, f"{{{KML_NS}}}name").text = name
    rep = {"simplified": False, "area_change_pct": 0.0, "polygons": 0}
    items = [(name, field_wgs84, "field")] + [(f"{name} exclusion {i + 1}", g, "exclusion")
                                              for i, g in enumerate(exclusions_wgs84)]
    for nm, g, kind in items:
        if g.geom_type == "MultiPolygon":
            g = max(g.geoms, key=lambda p: p.area)
        g2, simp = _simplify_to_limit(g, max_vertices)
        if simp:
            rep["simplified"] = True
            rep["area_change_pct"] = max(rep["area_change_pct"],
                                         abs(g2.area - g.area) / g.area * 100)
        pm = ET.SubElement(doc, f"{{{KML_NS}}}Placemark")
        ET.SubElement(pm, f"{{{KML_NS}}}name").text = nm
        ET.SubElement(pm, f"{{{KML_NS}}}description").text = kind
        pg = ET.SubElement(pm, f"{{{KML_NS}}}Polygon")
        ob = ET.SubElement(pg, f"{{{KML_NS}}}outerBoundaryIs")
        lr = ET.SubElement(ob, f"{{{KML_NS}}}LinearRing")
        coords = _ring_coords(g2)
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        ET.SubElement(lr, f"{{{KML_NS}}}coordinates").text = " ".join(
            f"{x:.7f},{y:.7f},0" for x, y in coords)
        rep["polygons"] += 1
    path = Path(path)
    ET.ElementTree(kml).write(path, encoding="utf-8", xml_declaration=True)
    if report is not None:
        report.update(rep)
    return path


def settings_sheet(settings, spec, region=None) -> dict:
    """What the operator enters in Agri Assistant for this plan."""
    sheet = {
        "aircraft": spec.name, "flight_controller": spec.fc.model,
        "work_height_m": settings.height_m, "work_speed_ms": settings.speed_ms,
        "row_spacing_m": settings.swath_m,
        "dose_l_per_ha": settings.rate_l_ha,
        "dose_l_per_mu": round(settings.rate_l_ha * MU_M2 / 10_000.0, 3),
        "dose_gal_per_acre": round(settings.rate_l_ha * ACRE_M2 / 10_000.0 / 3.785411784, 3),
        "flow_mode": "linkage (flow proportional to speed)",
        "rtk": bool(settings.rtk), "route_angle_deg": settings.route_angle_deg,
        "pump_l_min_max": settings.pump_lpm,
        "notes": ["Import the KML (one file, polygons only) from the phone's kitJson folder.",
                  "Breakpoint resume on: refill/battery swap returns to the last sprayed point."],
    }
    if region is not None:
        sheet["regulation"] = {"pack": region.id, "verified": region.rules.verified,
                               "wind_max_ms": region.rules.wind_max_ms,
                               "buffer_m": region.rules.buffer_m, "records": list(region.rules.records)}
        sheet["notes"] += region.warnings()
    return sheet


@dataclass
class JobRecord:
    start: dt.datetime | None
    flight_min: float | None
    area_ha: float
    total_l: float | None
    dose_l_ha: float | None
    fc_serial: str = ""
    distance_m: float | None = None
    crop: str = ""
    extra: dict = field(default_factory=dict)


_ALIASES = {
    "start_time": ("start_time", "start", "starttime", "time", "开始时间"),
    "flight_time_min": ("flight_time_min", "flight_time", "minutes", "flighttime", "飞行时间"),
    "sprayed_area_mu": ("sprayed_area_mu", "area_mu", "作业面积", "area(mu)"),
    "sprayed_area_ha": ("sprayed_area_ha", "area_ha", "area(ha)"),
    "sprayed_area_acre": ("sprayed_area_acre", "area_acre", "area(acre)"),
    "dose_l_per_mu": ("dose_l_per_mu", "亩用量", "dose_per_mu"),
    "total_l": ("total_l", "total_dose", "litres", "liters", "总用量"),
    "fc_serial": ("fc_serial", "serial", "fc_sn", "飞控序列号"),
    "distance_m": ("distance_m", "distance", "飞行距离"),
    "crop": ("crop", "作物"),
}


def _find(row: dict, key: str, mapping: dict | None):
    if mapping and key in mapping:
        return row.get(mapping[key])
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for a in _ALIASES.get(key, (key,)):
        if a.lower() in low:
            return low[a.lower()]
    return None


def _num(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return float(v)


def import_job_records(source, mapping: dict | None = None) -> tuple[list[JobRecord], list[str]]:
    """Parse JIYI job records. ``source`` is CSV/JSON *text*, or a ``pathlib.Path``
    to a file. A ``str`` is never interpreted as a path: this function sits behind
    an API, and user text must not be able to name a server file.
    -> (records, errors)."""
    text = source.read_text() if isinstance(source, Path) else str(source)
    text = text.strip()
    rows = json.loads(text) if text.startswith("[") or text.startswith("{") else list(
        csv.DictReader(io.StringIO(text)))
    if isinstance(rows, dict):
        rows = rows.get("records") or rows.get("data") or [rows]
    out, errors = [], []
    for i, row in enumerate(rows, start=1):
        try:
            st = _find(row, "start_time", mapping)
            start = dt.datetime.fromisoformat(str(st).replace("/", "-")) if st else None
            a_mu = _num(_find(row, "sprayed_area_mu", mapping))
            a_ha = _num(_find(row, "sprayed_area_ha", mapping))
            a_ac = _num(_find(row, "sprayed_area_acre", mapping))
            if a_ha is None:
                a_ha = a_mu * MU_M2 / 1e4 if a_mu is not None else (
                    a_ac * ACRE_M2 / 1e4 if a_ac is not None else None)
            if a_ha is None or a_ha <= 0:
                raise ValueError("no positive sprayed area")
            total = _num(_find(row, "total_l", mapping))
            dmu = _num(_find(row, "dose_l_per_mu", mapping))
            dose = (dmu * 1e4 / MU_M2) if dmu is not None else (total / a_ha if total else None)
            fm = _num(_find(row, "flight_time_min", mapping))
            out.append(JobRecord(start, fm, a_ha, total, dose,
                                 str(_find(row, "fc_serial", mapping) or ""),
                                 _num(_find(row, "distance_m", mapping)),
                                 str(_find(row, "crop", mapping) or ""),
                                 extra={k: v for k, v in row.items()}))
        except (ValueError, TypeError) as e:
            errors.append(f"row {i}: {e}")
    return out, errors
