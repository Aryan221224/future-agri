"""Spray records (JIYI import, manual, compliance), exports, and the farmer's Today card."""
import json
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from fa_console.app import create_app
from fa_console.config import Settings
from test_console_farms import _create, app, fake_builder  # noqa: F401  (fixture)
from test_console_plans import _inject_result, _result

CSV = ("start_time,area_ha,total_l,flight_time_min,fc_serial\n"
       "2025-06-01 06:10,1.0,15.0,12.5,K3A-0001\n"
       "2025-06-08 06:00,0.5,7.5,6.0,K3A-0001\n"
       "2025-06-15 06:00,0,0,1,K3A-0001\n")


def test_jiyi_import_reports_bad_rows(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        r = c.post(f"/api/farms/{f['id']}/records/import",
                   files={"file": ("jobs.csv", CSV.encode(), "text/csv")})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["added"] == 2 and out["report"]["rows"] == 3
        assert any(e.startswith("row 3") for e in out["report"]["errors"])
        recs = c.get(f"/api/farms/{f['id']}/records").json()["records"]
        assert len(recs) == 2 and recs[0]["source"] == "jiyi" and recs[0]["litres"] == 15.0
        assert "wind_speed_ms" in recs[0]["missing"]          # EU record keeping asks for wind


def test_manual_record_compliance(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        r = c.post(f"/api/farms/{f['id']}/records", json={
            "ts": "2025-06-02T06:30:00+00:00", "product": "fungicide", "litres": 15, "area_ha": 1.0,
            "operator": "A. Farmer", "aircraft_id": "K3A-01", "wind_ms": 1.4, "wind_dir_deg": 250,
            "temp_c": 16})
        assert r.status_code == 200, r.text
        assert r.json()["missing"] == []
        bad = c.post(f"/api/farms/{f['id']}/records", json={"ts": "2025-06-02T06:30:00", "product": "magic",
                                                            "litres": 1, "area_ha": 1})
        assert bad.status_code == 422


def test_upload_too_large_413(tmp_path):
    a = create_app(Settings(home=tmp_path / "h", cache=tmp_path / "c", offline=True, max_upload_mb=1),
                   start_workers=False)
    with TestClient(a) as c:
        r = c.post("/api/farms/x/records/import",
                   files={"file": ("big.csv", b"x" * (2 * 1024 * 1024), "text/csv")})
        assert r.status_code == 413


def test_kml_parses_and_has_polygon(app):
    with TestClient(app) as c:
        f, _ = _create(c, app)
        r = c.get(f"/api/farms/{f['id']}/export/kml")
        assert r.status_code == 200 and "kml" in r.headers["content-type"]
        root = ET.fromstring(r.content)
        coords = [e.text for e in root.iter() if e.tag.endswith("coordinates")]
        assert coords and len(coords[0].split()) >= 4
        s = c.get(f"/api/farms/{f['id']}/export/survey.kml")
        assert s.status_code == 200 and b"altitude_m" in s.content
        st = c.get(f"/api/farms/{f['id']}/export/settings").json()
        assert st["flight_controller"] == "JIYI K3-A Pro" and st["dose_l_per_ha"] > 0


def test_ics_csv_pdf_from_approved_plan(app):
    res = _result()
    app.state.sessions.defaults["plan_members"] = 4
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        jid = _inject_result(app, fid, res)
        twin_plan = next(r["plan"] for r in res["ranked"] if r["policy"].startswith("twin"))
        p = c.post(f"/api/farms/{fid}/plans", json={"job_id": jid, "plan": twin_plan}).json()
        assert c.post(f"/api/plans/{p['id']}/approve", json={}).status_code == 200
        ics = c.get(f"/api/farms/{fid}/export/plan.ics")
        assert ics.status_code == 200 and ics.text.startswith("BEGIN:VCALENDAR")
        assert ics.text.count("BEGIN:VEVENT") == len(p["payload"]["operations"]) >= 1
        csv_ = c.get(f"/api/farms/{fid}/export/plan.csv").text
        assert csv_.splitlines()[0].startswith("product,") and len(csv_.splitlines()) == 1 + len(p["payload"]["operations"])
        pdf = c.get(f"/api/farms/{fid}/export/plan.pdf")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        z = c.get(f"/api/farms/{fid}/export/bundle")
        assert z.status_code == 200 and z.content[:2] == b"PK"


def test_today_codes_cover_states(app):
    app.state.sessions.defaults["plan_members"] = 4
    with TestClient(app) as c:
        f, _ = _create(c, app)
        fid = f["id"]
        t = c.get(f"/api/farms/{fid}/today").json()
        assert t["status"] == "none" and t["code"] == "no_action" and t["farm"]["crop"] == "potato"
        c.post(f"/api/farms/{fid}/session/run", json={"stop_on_decision": True})
        app.state.jobs.run_pending()
        t = c.get(f"/api/farms/{fid}/today").json()
        assert t["status"] == "wait" and t["code"] == "wait_approval" and t["next"]["product"]
        op = next(o for o in c.get(f"/api/farms/{fid}/session").json()["ops"] if o["status"] == "proposed")
        c.post(f"/api/farms/{fid}/session/approve", json={"op_id": op["id"]})
        t = c.get(f"/api/farms/{fid}/today").json()
        if op["due"] <= t["day"]:                        # due now: spray, or wait for the weather
            assert t["code"] in ("spray_today", "wait_weather") and t["status"] in ("spray", "wait")
        else:                                            # approved for later this week
            assert t["code"] == "no_action" and t["next"]["op"] == op["id"]
        assert "stage_code" in t["health"]
        c.post(f"/api/farms/{fid}/session/run", json={"stop_on_decision": False, "approve_all": True})
        app.state.jobs.run_pending()
        t = c.get(f"/api/farms/{fid}/today").json()
        assert t["code"] == "season_over"
        assert json.dumps(t)
