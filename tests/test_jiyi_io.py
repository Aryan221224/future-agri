"""Task 8: KML export for JIYI Agri Assistant; job-record import."""
import math
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from shapely.geometry import Point, box

from fa_drone.jiyi_io import export_kml, import_job_records, settings_sheet
from fa_drone.mission import MissionSettings
from fa_drone.spec import load_spec

NS = {"k": "http://www.opengis.net/kml/2.2"}


def _poly(lat=52.52, lon=5.62, w=200, h=100):
    dlat = h / 111_320
    dlon = w / (111_320 * math.cos(math.radians(lat)))
    return box(lon, lat, lon + dlon, lat + dlat)


def test_kml_one_polygon_closed_ring(tmp_path):
    p = export_kml(_poly(), [], "Field 1", tmp_path / "f.kml")
    root = ET.parse(p).getroot()
    polys = root.findall(".//k:Polygon", NS)
    assert len(polys) == 1
    coords = polys[0].find(".//k:coordinates", NS).text.split()
    assert coords[0] == coords[-1]
    lon, lat, _ = map(float, coords[0].split(","))
    assert 5.6 < lon < 5.7 and 52.5 < lat < 52.6


def test_kml_exclusions_are_separate_polygons(tmp_path):
    f = _poly()
    hole = f.centroid.buffer(0.0002)
    root = ET.parse(export_kml(f, [hole], "Field", tmp_path / "f.kml")).getroot()
    assert len(root.findall(".//k:Polygon", NS)) == 2


def test_kml_simplifies_to_waypoint_limit(tmp_path):
    circle = Point(5.62, 52.52).buffer(0.003, quad_segs=300)      # ~1,200 vertices
    report = {}
    root = ET.parse(export_kml(circle, [], "Round", tmp_path / "c.kml", report=report)).getroot()
    n = len(root.find(".//k:coordinates", NS).text.split())
    assert n <= 400 and report["simplified"] is True
    assert report["area_change_pct"] < 1.0


def test_settings_sheet_units():
    s = settings_sheet(MissionSettings(rate_l_ha=15.0, swath_m=4.0, speed_ms=5.0, height_m=2.5),
                       load_spec())
    assert s["dose_l_per_mu"] == pytest.approx(1.0, abs=0.01)
    assert s["flow_mode"] == "linkage (flow proportional to speed)"
    assert s["row_spacing_m"] == 4.0


def test_import_job_records_csv_mu_to_ha():
    text = ("start_time,flight_time_min,sprayed_area_mu,dose_l_per_mu,total_l,fc_serial,distance_m,crop\n"
            "2025-06-10 07:12,11.5,9.0,1.0,9.0,K3A123,2100,potato\n"
            "2025-06-10 07:40,bad,9.0,1.0,9.0,K3A123,2050,potato\n")
    recs, errors = import_job_records(text)
    assert len(recs) == 1 and len(errors) == 1
    r = recs[0]
    assert r.area_ha == pytest.approx(0.6, abs=0.001)
    assert r.dose_l_ha == pytest.approx(15.0, abs=0.01)
    assert "row 2" in errors[0]


def test_import_job_records_json_with_mapping():
    js = '[{"Start": "2025-06-10T07:12:00", "Area(ha)": 0.67, "Litres": 10, "Minutes": 12}]'
    recs, errors = import_job_records(js, mapping={"start_time": "Start", "sprayed_area_ha": "Area(ha)",
                                                   "total_l": "Litres", "flight_time_min": "Minutes"})
    assert not errors and recs[0].total_l == 10 and recs[0].area_ha == pytest.approx(0.67)
