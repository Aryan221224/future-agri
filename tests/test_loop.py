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


# --- review #1: the operational path must work when the future is unknown ---

def _observed(start, end, seed=11):
    recs = generate_records(start, (end - start).days + 1, seed)
    return [dict(r, DAY=r["DAY"].isoformat()) for r in recs]


def test_api_advice_on_observed_weather_ending_today():
    from fastapi.testclient import TestClient
    from fa_api.main import app
    c = TestClient(app)
    obs = _observed(dt.date(2025, 10, 1), dt.date(2025, 12, 5))
    r = c.post("/twin/obs1/setup", json={"sowing_date": "2025-10-25", "harvest_date": "2026-02-10",
                                         "weather": obs})
    assert r.status_code == 200, r.text
    r = c.post("/twin/obs1/advice", json={"day": "2025-12-06"})          # weather known to 5 Dec
    assert r.status_code == 200, r.text
    assert "climatology" in r.json()["forecast"]
    r = c.post("/twin/obs1/advice", json={"day": "2025-12-10"})          # gap: 6-9 Dec unknown
    assert r.status_code == 422 and "weather" in r.text.lower()
    more = _observed(dt.date(2025, 12, 6), dt.date(2025, 12, 9), seed=12)
    assert c.post("/twin/obs1/weather", json=more).status_code == 200
    r = c.post("/twin/obs1/advice", json={"day": "2025-12-10"})
    assert r.status_code == 200, r.text


def test_api_accepts_a_15_day_forecast_ensemble_and_warns_when_small():
    from fastapi.testclient import TestClient
    from fa_api.main import app
    c = TestClient(app)
    obs = _observed(dt.date(2025, 10, 1), dt.date(2025, 12, 5))
    assert c.post("/twin/obs2/setup", json={"sowing_date": "2025-10-25", "harvest_date": "2026-02-10",
                                            "weather": obs}).status_code == 200
    fc = [_observed(dt.date(2025, 12, 6), dt.date(2025, 12, 20), seed=40 + j) for j in range(3)]
    r = c.post("/twin/obs2/advice", json={"day": "2025-12-06", "forecast": fc})
    assert r.status_code == 200, r.text
    assert "3 forecast members" in r.json()["forecast"]


def test_wheat_gets_irrigation_advice_but_no_potato_blight_or_herbicide_advice():
    """Review #5: the blight model and economics are potato's; wheat must be refused, not advised."""
    from fastapi.testclient import TestClient
    from fa_api.main import app
    c = TestClient(app)
    r = c.post("/twin/w2/setup", json={"crop": "wheat", "variety": "IN_NW_timely",
                                       "sowing_date": "2025-11-15", "harvest_date": "2026-04-30",
                                       "synthetic_weather_seed": 2})
    assert r.status_code == 200, r.text
    a = c.post("/twin/w2/advice", json={"day": "2026-01-10"}).json()
    assert a["fungicide"]["spray_day"] is None
    assert "not modelled" in a["fungicide"]["reason"]
    assert a["scout"] is False
    assert isinstance(a["irrigate"]["action"], bool)
    p = c.post("/twin/w2/pass", json={"day": "2026-01-10", "canopy_cover": 0.5,
                                      "weed_density_cells": [10, 12]}).json()
    assert "herbicide" not in p or p["herbicide"] == "not modelled"


def test_camera_pass_goes_through_the_ensemble_filter():
    """Review #2: the operational pass uses the EnKF (cover + stage), not a hard LAI overwrite."""
    obs = generate_records(dt.date(2025, 10, 1), 70, 21)       # to 9 Dec
    cfg = ZoneConfig(zone_id="p", latitude=28.45, longitude=77.58, crop="potato",
                     variety="Potato_701", sowing_date=dt.date(2025, 10, 25),
                     harvest_date=dt.date(2026, 2, 10))
    tw = FieldTwin(cfg, obs)
    day = dt.date(2025, 12, 9)
    tw.advance_to(day)
    lai0 = tw.state()["LAI"]
    cc = float(1 - np.exp(-0.6 * lai0 * 1.4))                   # field greener than the twin
    d = tw.ingest_pass(day, cc, growth_stage_dvs=tw.state()["DVS"], canopy_cover_sd=0.04)
    assert d.data["method"] == "EnKF"
    lai1 = tw.state()["LAI"]
    assert lai0 < lai1 < -np.log(1 - cc) / 0.6 + 1e-6         # moved toward, not onto, the inversion
    assert "posterior_parameters" in d.data


def test_review_labels_persist_and_feed_action_gate_recalibration():
    """Review #8: labels survive a restart and recalibrate the gate the pipeline uses."""
    from fastapi.testclient import TestClient
    from fa_api import advisory
    from fa_api.main import app
    from fa_core.uncertainty import ActionGate, calibrate_action_gate, recalibrate_action_gate
    from fa_gateway.pipeline import ReviewItem
    classes = ["maize", "amaranth", "quickweed", "background"]
    rng = np.random.default_rng(5)
    y = rng.choice(4, 600, p=[0.4, 0.3, 0.1, 0.2])
    lg = rng.normal(0, 1, (600, 4))
    lg[np.arange(600), y] += 2.0
    p = np.exp(lg) / np.exp(lg).sum(1, keepdims=True)
    gate = calibrate_action_gate(p[:300], y[:300], classes, alpha=0.10)
    names = list(gate.groups)
    to_action = {c: g for g, cs in gate.groups.items() for c in cs}

    c = TestClient(app)
    items = [ReviewItem(f"f{i}", "z", (0, 0, 5, 5), ["crop", "weed"], classes[int(y[300 + i])],
                        "action unclear", "2025-12-01", probs=list(map(float, p[300 + i]))).to_payload()
             for i in range(120)]
    assert c.post("/twin/plotR/review", json=items).json()["queued"] == 120
    for i in range(120):
        c.post(f"/twin/plotR/review/{i}", params={"label": to_action[classes[int(y[300 + i])]]})
    on_disk = advisory._load_review()["plotR"]                  # what a restarted API would load
    assert sum(it["status"] == "labelled" for it in on_disk) == 120
    lab = c.get("/twin/plotR/review/labels").json()
    assert lab["n_usable"] == 120 and lab["recalibration"] == "possible"
    new, rep = recalibrate_action_gate(gate, np.array(lab["probs"]), lab["labels"])
    assert isinstance(new, ActionGate) and rep.used_field_labels and new is not gate
    kept, rep2 = recalibrate_action_gate(gate, np.array(lab["probs"][:20]), lab["labels"][:20])
    assert kept is gate and not rep2.used_field_labels
    with pytest.raises(ValueError):
        recalibrate_action_gate(gate, np.array(lab["probs"]), ["banana"] * 120)
    assert set(names) == {"crop", "weed", "background"}
