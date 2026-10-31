"""Task 12: HTTP surface for the farm, the drone twin and job records."""
import math

from fastapi.testclient import TestClient
from shapely.geometry import box, mapping

from fa_api.main import app

client = TestClient(app)


def _poly(w=100, h=80, lat=52.52, lon=5.62):
    dlat = h / 111_320
    dlon = w / (111_320 * math.cos(math.radians(lat)))
    return mapping(box(lon, lat, lon + dlon, lat + dlat))


def test_archetypes_and_regions():
    a = client.get("/farm/archetypes").json()
    assert a["A7"]["supported"] is False and a["A5"]["crop"] == "potato"
    r = client.get("/farm/regions").json()
    assert r["BR"]["verified"] is True and r["JP"]["verified"] is False


def test_drone_simulate_ok_and_windy():
    ok = client.post("/drone/simulate", json={"polygon": _poly(), "region": "EU"}).json()
    assert ok["feasible"] and ok["kpis"]["litres"] > 0
    bad = client.post("/drone/simulate", json={"polygon": _poly(), "weather": {"wind_ms": 9, "gust_ms": 12}}).json()
    assert bad["feasible"] is False and bad["reasons"]


def test_drone_simulate_bad_polygon_422():
    r = client.post("/drone/simulate", json={"polygon": {"type": "Point", "coordinates": [5, 52]}})
    assert r.status_code == 422


def test_kml_endpoint():
    r = client.post("/drone/kml", json={"polygon": _poly(), "region": "BR"}).json()
    assert "<Polygon>" in r["kml"] and r["settings"]["regulation"]["buffer_m"] == 20


def test_job_records_endpoint():
    txt = "start_time,sprayed_area_ha,total_l\n2025-06-01 07:00,0.6,9\n"
    r = client.post("/drone/job-records", json={"text": txt}).json()
    assert r["records"][0]["area_ha"] == 0.6 and r["errors"] == []


def test_unknown_archetype_404():
    assert client.post("/farm/build", json={"archetype": "Z9", "year": 2025}).status_code == 404


def test_job_records_endpoint_does_not_read_server_files(tmp_path):
    f = tmp_path / "server_side.csv"
    f.write_text("start_time,sprayed_area_ha,total_l\n2025-06-01 07:00,0.6,9\n")
    r = client.post("/drone/job-records", json={"text": str(f)}).json()
    assert r.get("records", []) == []
