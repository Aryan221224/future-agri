"""Task 9: the separate camera drone's scan and the DigitalFarm it builds."""
import datetime as dt
import pickle

import numpy as np
import pytest

from fa_farm.truth import GroundTruthFarm
from fa_scan.digital import DigitalFarm, build_digital_farm
from fa_scan.measure import DetectorModel, simulate_scan
from fa_scan.survey import plan_scan
from test_truth import NL_NORMALS, _spec, _weather


@pytest.fixture(scope="module")
def truth():
    t = GroundTruthFarm(_spec(w=150, h=100, cell=5.0, inoculum=False, seed=7),
                        _weather(dt.date(2025, 1, 1), 365, NL_NORMALS))
    t.advance_to(dt.date(2025, 6, 5))
    return t


def test_altitude_for_gsd_matches_geometry(truth):
    p = plan_scan(truth.grid, "p4rtk", gsd_m=0.010)
    assert p.altitude_m == pytest.approx(0.010 * 8.8 * 5472 / 13.2, abs=1.0)
    assert p.frames > 0 and p.flight_min > 0 and p.batteries >= 1


def test_scan_time_grows_as_gsd_shrinks(truth):
    assert plan_scan(truth.grid, "p4rtk", 0.005).flight_min > plan_scan(truth.grid, "p4rtk", 0.02).flight_min


def test_measured_recall_falls_with_coarser_gsd():
    det = DetectorModel.load()
    assert det.weed_recall(size_cm=4.0, gsd_m=0.0025) > det.weed_recall(4.0, 0.010) > det.weed_recall(4.0, 0.03)
    assert 0.6 < det.weed_recall(10.0, 0.0025) < 0.95          # measured large-object recall
    assert det.weed_recall(1.0, 0.05) < 0.05


def test_scan_cover_close_to_truth(truth):
    rng = np.random.default_rng(0)
    plan = plan_scan(truth.grid, "p4rtk", gsd_m=0.010)
    obs = simulate_scan(truth.state(), truth.grid, plan, rng, crop="potato",
                        days_after_sowing=(truth.day - truth.sowing).days)
    st = truth.state()
    assert abs(obs.cc_cells.mean() - st.cc.mean()) < 0.05
    assert obs.cc_sd > 0 and obs.gsd_m == 0.010


def test_digital_farm_zones_and_weed_map(truth):
    rng = np.random.default_rng(1)
    plan = plan_scan(truth.grid, "p4rtk", gsd_m=0.005)
    obs = simulate_scan(truth.state(), truth.grid, plan, rng, crop="potato", days_after_sowing=46)
    df = build_digital_farm(obs, truth.grid, k=3)
    assert isinstance(df, DigitalFarm) and 1 <= len(df.zones) <= 3
    assert sum(z.area_ha for z in df.zones) == pytest.approx(truth.grid.area_ha, rel=1e-6)
    st = truth.state()
    # the recall-corrected weed estimate tracks the true mean density
    assert df.weed_map.mean() == pytest.approx(st.weeds_m2.mean(), rel=0.5)
    assert df.weed_map_sd.shape == df.weed_map.shape


def test_digital_farm_never_holds_truth(truth):
    rng = np.random.default_rng(2)
    obs = simulate_scan(truth.state(), truth.grid, plan_scan(truth.grid, "p4rtk", 0.01), rng,
                        crop="potato", days_after_sowing=46)
    df = build_digital_farm(obs, truth.grid)
    blob = pickle.dumps(df)
    assert b"GroundTruthFarm" not in blob and b"CellState" not in blob


def _sweep():
    import json
    from fa_scan.measure import DATA
    return json.loads((DATA / "detector_wg_medium_gsd.json").read_text())


def test_recall_reproduces_the_h200_gsd_sweep():
    """On the test set's own plant sizes, the model gives the measured recall at every GSD."""
    sw = _sweep()
    det = DetectorModel.load()
    ref = sw["by_factor"]["1"]["weed"]
    mids_px = [6, 14, 18, 22, 28, 40, 56, 80, 160]
    sizes_cm = [m * sw["gsd_ref_m"] * 100 for m in mids_px]          # the same plants at every GSD
    for k, st in sw["by_factor"].items():
        g = sw["gsd_ref_m"] * int(k)
        pred = sum(n * det.weed_recall(s, g) for n, s in zip(ref["n"], sizes_cm)) / sum(ref["n"])
        meas = st["weed"]["overall_recall_right_group"]
        assert abs(pred - meas) < 0.05, (k, round(pred, 3), round(meas, 3))


def test_recall_falls_with_gsd_for_the_same_plant():
    det = DetectorModel.load()
    r = [det.weed_recall(4.0, g) for g in (0.001, 0.0025, 0.005, 0.01, 0.02, 0.04)]
    assert all(a >= b for a, b in zip(r, r[1:])) and r[0] > 0.6 and r[-1] < 0.02


def test_false_positives_follow_the_sweep():
    sw = _sweep()
    det = DetectorModel.load()
    for k, st in sw["by_factor"].items():
        assert abs(det.fp_per_m2(sw["gsd_ref_m"] * int(k)) - st["weed_fp_per_m2"]) < 1e-6
    assert det.fp_per_m2(0.0075) < det.fp_per_m2(0.005)               # interpolated between factors
