"""Exports: K3-A Pro KML + settings sheet, camera survey KML, plan CSV / ICS / PDF,
records CSV, and a farm bundle (zip)."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict
from pathlib import Path

from fa_drone.jiyi_io import KML_NS, _ring_coords, _simplify_to_limit, settings_sheet
from fa_drone.mission import MissionSettings
from fa_drone.spec import load_spec
from fa_farm.regions import load_region
from fa_scan.survey import plan_scan

from .errors import NotFound
from .scene import farm_grid


def kml_polygons(name: str, polys_wgs84: list, extended: dict | None = None, max_vertices: int = 400) -> bytes:
    """KML with one Placemark Polygon per area (what Agri Assistant imports)."""
    ET.register_namespace("", KML_NS)
    k = lambda t: f"{{{KML_NS}}}{t}"  # noqa: E731
    kml = ET.Element(k("kml"))
    doc = ET.SubElement(kml, k("Document"))
    ET.SubElement(doc, k("name")).text = name
    if extended:
        ed = ET.SubElement(doc, k("ExtendedData"))
        for key, v in extended.items():
            d = ET.SubElement(ed, k("Data"), name=str(key))
            ET.SubElement(d, k("value")).text = str(v)
    for i, g in enumerate(polys_wgs84):
        if g.geom_type == "MultiPolygon":
            g = max(g.geoms, key=lambda p: p.area)
        g2, _ = _simplify_to_limit(g, max_vertices)
        pm = ET.SubElement(doc, k("Placemark"))
        ET.SubElement(pm, k("name")).text = name if len(polys_wgs84) == 1 else f"{name} area {i + 1}"
        lr = ET.SubElement(ET.SubElement(ET.SubElement(pm, k("Polygon")), k("outerBoundaryIs")), k("LinearRing"))
        coords = _ring_coords(g2)
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        ET.SubElement(lr, k("coordinates")).text = " ".join(f"{x:.7f},{y:.7f},0" for x, y in coords)
    return ET.tostring(kml, encoding="utf-8", xml_declaration=True)


class ExportService:
    def __init__(self, store, settings, farms, sessions):
        self.store, self.settings, self.farms, self.sessions = store, settings, farms, sessions

    def _plan(self, fid: str) -> dict | None:
        p = self.store.approved_plan(fid)
        return p["payload"] if p else None

    def _flight(self, fid: str) -> MissionSettings:
        return MissionSettings(**self.sessions.config(fid)["flight"])

    def _target_polys(self, fid: str, op_id: str):
        ops = {o["id"]: o for o in self.store.list_operations(fid)}
        if op_id not in ops:
            raise NotFound(f"no operation {op_id}")
        op = self.sessions.get(fid).ops.get(op_id)
        if op is None or op.targets is None:
            return [self.farms.load(fid).spec.polygon_wgs84]
        return self._polys_for(fid, op.targets)

    def _polys_for(self, fid: str, targets):
        from pyproj import Transformer
        from shapely.geometry import box
        from shapely.ops import transform as shp_transform, unary_union
        grid = farm_grid(self.farms.load(fid))
        h = grid.cell_m / 2
        cells = [box(x - h, y - h, x + h, y + h) for (x, y), t in zip(grid.cells_xy, targets) if t]
        if not cells:
            raise ValueError("nothing left to spray")
        u = unary_union(cells).buffer(0.01)
        tr = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
        u = shp_transform(tr.transform, u)
        return list(u.geoms) if u.geom_type == "MultiPolygon" else [u]

    def targets_kml(self, fid: str, name: str, targets) -> bytes:
        """KML of the given target cells (e.g. the next sortie of a live run) for the aircraft app."""
        return kml_polygons(name, self._polys_for(fid, targets))

    def kml(self, fid: str, op_id: str | None = None) -> bytes:
        farm = self.farms.get(fid)
        vf = self.farms.load(fid)
        polys = self._target_polys(fid, op_id) if op_id else [vf.spec.polygon_wgs84]
        name = f"{farm['name']}" + (f" - {op_id}" if op_id else "")
        return kml_polygons(name, polys)

    def sheet(self, fid: str) -> dict:
        farm = self.farms.get(fid)
        sheet = settings_sheet(self._flight(fid), load_spec(), load_region(farm["region"]))
        plan = self._plan(fid)
        sheet["plan"] = plan["plan"] if plan else "default (no approved plan)"
        sheet["farm"] = farm["name"]
        return sheet

    def survey_kml(self, fid: str, gsd_m: float = 0.01) -> bytes:
        farm = self.farms.get(fid)
        vf = self.farms.load(fid)
        sp = plan_scan(farm_grid(vf), "p4rtk", gsd_m)
        ext = {"camera": sp.camera, "gsd_m": sp.gsd_m, "altitude_m": round(sp.altitude_m, 1),
               "line_spacing_m": round(sp.line_spacing_m, 1), "speed_ms": round(sp.speed_ms, 1),
               "front_overlap": sp.front_overlap, "side_overlap": sp.side_overlap, "frames": sp.frames,
               "flight_min": round(sp.flight_min, 1), "batteries": sp.batteries}
        return kml_polygons(f"{farm['name']} - survey", [vf.spec.polygon_wgs84], ext)

    def _ops_rows(self, fid: str) -> list[dict]:
        plan = self._plan(fid)
        if plan:
            return plan["operations"]
        return [{"product": o["product"], "date_first": o["due"], "date_last": o["due"], "count_mean": 1,
                 "status": o["status"]} for o in self.store.list_operations(fid)
                if o["status"] in ("proposed", "approved")]

    def plan_csv(self, fid: str) -> str:
        self.farms.get(fid)
        rows = self._ops_rows(fid)
        cols = ["product", "count_mean", "date_first", "date_last", "das_first", "das_median", "das_last",
                "time_min", "crew_min", "sorties", "battery_swaps", "refills", "litres", "area_ha", "cv_pct",
                "gsd_m", "status"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return buf.getvalue()

    def plan_ics(self, fid: str) -> str:
        farm = self.farms.get(fid)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Future Agri//Mission Console//EN",
                 "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{_ics(farm['name'])}"]
        for i, r in enumerate(self._ops_rows(fid)):
            d0 = dt.date.fromisoformat(r["date_first"])
            d1 = dt.date.fromisoformat(r["date_last"]) + dt.timedelta(days=1)
            what = r["product"].capitalize()
            desc = (f"Expected {r.get('count_mean', 1)} x; drone {r.get('time_min', '?')} min, crew "
                    f"{r.get('crew_min', '?')} min, {r.get('litres', '?')} L each") if r["product"] != "scan" \
                else f"Camera-drone scan at {1000 * (r.get('gsd_m') or 0):.0f} mm GSD"
            lines += ["BEGIN:VEVENT", f"UID:{fid}-{i}-{r['product']}@futureagri", f"DTSTAMP:{stamp}",
                      f"DTSTART;VALUE=DATE:{d0:%Y%m%d}", f"DTEND;VALUE=DATE:{d1:%Y%m%d}",
                      f"SUMMARY:{_ics(what + ' window - ' + farm['name'])}", f"DESCRIPTION:{_ics(desc)}",
                      "END:VEVENT"]
        lines.append("END:VCALENDAR")
        return "\r\n".join(lines) + "\r\n"

    def plan_pdf(self, fid: str) -> bytes:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        farm = self.farms.get(fid)
        plan = self._plan(fid)
        sheet = self.sheet(fid)
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, title=f"Plan - {farm['name']}", author="Future Agri")
        ss = getSampleStyleSheet()
        el = [Paragraph(f"Season plan: {farm['name']}", ss["Title"]),
              Paragraph(f"{farm['crop']} · {farm['area_ha'] or 0:.2f} ha · season {farm['sowing']} to {farm['harvest']} · "
                        f"region {farm['region']} · {'virtual test farm' if farm['mode'] == 'sim' else 'live field'}",
                        ss["Normal"]), Spacer(1, 10)]
        if plan:
            el += [Paragraph(f"Plan <b>{plan['plan']}</b> (policy {plan['policy']}): expected net "
                             f"{plan['net_mean']:.0f} {plan['currency']}/ha, bad-case (CVaR20) {plan['cvar20']:.0f}",
                             ss["Normal"]), Spacer(1, 8)]
        st = TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8efe6")),
                         ("FONTSIZE", (0, 0), (-1, -1), 8)])
        el += [Paragraph("K3-A Pro settings (enter in Agri Assistant)", ss["Heading2"]),
               Table([["height m", "speed m/s", "row spacing m", "L/ha", "L/mu", "RTK"],
                      [sheet["work_height_m"], sheet["work_speed_ms"], sheet["row_spacing_m"], sheet["dose_l_per_ha"],
                       sheet["dose_l_per_mu"], "yes" if sheet["rtk"] else "no"]], style=st), Spacer(1, 8)]
        rows = self._ops_rows(fid)
        el.append(Paragraph("Operations", ss["Heading2"]))
        data = [["operation", "expected", "window", "drone min", "crew min", "sorties", "litres"]]
        for r in rows:
            data.append([r["product"], r.get("count_mean", ""), f"{r['date_first']} - {r['date_last']}",
                         r.get("time_min", ""), r.get("crew_min", ""), r.get("sorties", ""), r.get("litres", "")])
        el += [Table(data, style=st), Spacer(1, 8)]
        if plan:
            t = plan["totals"]
            el.append(Paragraph(f"Season totals: {t['missions']} missions, {t['drone_h']} drone hours, {t['crew_h']} crew "
                                f"hours, {t['litres']} L, {t['scans']} scans.", ss["Normal"]))
            if plan.get("logistics_pick"):
                lp = plan["logistics_pick"]
                el.append(Paragraph(f"Batteries and chargers: {lp['battery_sets']} battery sets, {lp['chargers']} "
                                    f"charger(s) ({lp['why']}).", ss["Normal"]))
            for w in plan.get("warnings", []) + plan.get("assumptions", []):
                el.append(Paragraph(f"• {w}", ss["Normal"]))
        el.append(Spacer(1, 10))
        el.append(Paragraph("Simulated by the Future Agri digital twin from real data; not a field measurement.",
                            ss["Italic"]))
        doc.build(el)
        return buf.getvalue()

    def records_csv(self, fid: str, records: list[dict]) -> str:
        buf = io.StringIO()
        cols = ["ts", "source", "product", "litres", "area_ha", "operation_id", "missing"]
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(cols)
        for r in records:
            w.writerow([r.get("ts"), r.get("source"), r.get("product"), r.get("litres"), r.get("area_ha"),
                        r.get("operation_id"), ";".join(r.get("missing", []))])
        return buf.getvalue()

    def bundle_zip(self, fid: str) -> bytes:
        farm = self.farms.get(fid)
        d = self.store.farm_dir(fid)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("farm.json", json.dumps(farm, indent=1, default=str))
            z.writestr("plans.json", json.dumps(self.store.list_plans(fid), default=str))
            z.writestr("records.json", json.dumps(self.store.list_records(fid), default=str))
            z.writestr("session_actions.json", json.dumps(
                [e["payload"] for e in self.store.events(fid, kinds=["session_action"], limit=1_000_000)], default=str))
            z.writestr("events.json", json.dumps(self.store.events(fid, limit=100_000), default=str))
            for name in ("scene.json", "imagery.png", "farm.pkl", "season_result.json", "weather_extra.json"):
                if (d / name).exists():
                    z.write(d / name, name)
            for sub in ("scans", "tracks"):
                for f in sorted((d / sub).glob("*.json")) if (d / sub).exists() else []:
                    z.write(f, f"{sub}/{f.name}")
        return buf.getvalue()


def _ics(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")
