"""End to end: a full simulated season through the orchestrator, the API, and photo ingest."""
import datetime as dt

import numpy as np
import pytest

from fa_loop.orchestrator import FieldTwin
from fa_loop.season import simulate_season
from fa_twin.engine import ZoneConfig
from fa_twin.weather import generate_records


@pytest.fixture(scope="module")
def season_pair():
    # 10 members, the audit's setting. With 4, the planner under-sprayed an
    # infected season (4 sprays vs 8, final severity 0.86, -6.6 % yield).
    return (simulate_season(4, "farmer", plan_members=10),
            simulate_season(4, "twin", plan_members=10, keep_log=True))


def test_full_season_runs_and_twin_saves_water_at_equal_yield(season_pair):
    farmer, twin = season_pair
    assert farmer.water_cm == 40.0 and farmer.sprays >= 6
    assert twin.water_cm < farmer.water_cm
    assert abs(twin.fresh_t_ha_farm - farmer.fresh_t_ha_farm) / farmer.fresh_t_ha_farm < 0.05


def test_every_decision_is_logged_with_a_reason(season_pair):
    _, twin = season_pair
    kinds = {e["kind"] for e in twin.log}
    assert {"gate", "irrigate", "pass", "herbicide", "fungicide"} <= kinds
    assert all(e["detail"] for e in twin.log)
    passes = [e for e in twin.log if e["kind"] == "pass"]
    assert len(passes) == 1 and "cover" in passes[0]["detail"]


def test_infeasible_field_is_refused_before_any_advice():
    cfg = ZoneConfig(zone_id="w", latitude=28.45, longitude=77.58, crop="wheat",
                     variety="Winter_wheat_102", sowing_date=dt.date(2025, 11, 15),
                     harvest_date=dt.date(2026, 4, 30))
    with pytest.raises(ValueError, match="refused"):
        FieldTwin(cfg, generate_records(dt.date(2025, 11, 1), 200, 1))


def test_api_setup_pass_advice_and_log():
    from fastapi.testclient import TestClient
    from fa_api.main import app
    c = TestClient(app)
    r = c.post("/twin/plot7/setup", json={"sowing_date": "2025-10-25", "harvest_date": "2026-02-10",
                                          "synthetic_weather_seed": 3})
    assert r.status_code == 200, r.text
    assert "SYNTHETIC" in r.json()["weather_source"]
    r = c.post("/twin/plot7/pass", json={"day": "2025-11-29", "canopy_cover": 0.55,
                                         "growth_stage_dvs": 0.9,
                                         "weed_density_cells": [12, 8, 20, 3, 15]})
    assert r.status_code == 200 and r.json()["herbicide"] in ("none", "blanket", "spot")
    r = c.post("/twin/plot7/advice", json={"day": "2025-12-06"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert "climatology" in a["forecast"]
    assert isinstance(a["irrigate"]["action"], bool) and a["irrigate"]["reason"]
    assert a["fungicide"]["reason"]
    log = c.get("/twin/plot7/log").json()
    assert any(e["kind"] == "pass" for e in log)


def test_api_refuses_european_wheat_with_reasons():
    from fastapi.testclient import TestClient
    from fa_api.main import app
    r = TestClient(app).post("/twin/w1/setup", json={
        "crop": "wheat", "variety": "Winter_wheat_102", "sowing_date": "2025-11-15",
        "harvest_date": "2026-04-30", "synthetic_weather_seed": 1})
    assert r.status_code == 422
    assert "photoperiod" in str(r.json())


def test_one_pass_from_photos_measures_cover(tmp_path):
    from PIL import Image
    from fa_loop.ingest import one_pass_from_photos
    rng = np.random.default_rng(0)
    frames = {}
    for st, frac in (("S1", 0.3), ("S2", 0.6)):
        paths = []
        for k in range(3):
            img = np.zeros((200, 200, 3), np.uint8)
            img[...] = (120, 90, 60)                         # soil
            mask = rng.random((200, 200)) < frac
            img[mask] = (40, 160, 40)                        # canopy
            p = tmp_path / f"{st}_{k}.png"
            Image.fromarray(img).save(p)
            paths.append(p)
        frames[st] = paths
    obs = one_pass_from_photos(frames, bbch=40)
    assert 0.3 < obs.canopy_cover < 0.6
    assert obs.growth_stage_dvs == pytest.approx(1.0)
    assert obs.n_frames == 6


def test_review_queue_collects_field_labels():
    from fastapi.testclient import TestClient
    from fa_api.main import app
    c = TestClient(app)
    items = [{"frame_id": f"f{i}", "bbox": [0, 0, 10, 10], "label_set": ["crop", "weed"],
              "top_class": "amaranth", "reason": "action unclear: crop or weed"} for i in range(3)]
    assert c.post("/twin/plot9/review", json=items).json()["pending"] == 3
    r = c.post("/twin/plot9/review/1", params={"label": "weed"}).json()
    assert r["field_labels"] == 1 and "more field labels" in r["recalibration"]
    assert len(c.get("/twin/plot9/review").json()) == 2
    assert c.post("/twin/plot9/review/0", params={"label": "banana"}).status_code == 422
