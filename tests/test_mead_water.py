"""Mead water study: the transcribed farm records and the results file stay consistent."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ml" / "mead"))
W = pytest.importorskip("water")

# Season totals (inches, field average) as read from the AmeriFlux BADM DM_WATER comments.
EXPECTED_IN = {("Ne1", 2014): 8.10, ("Ne1", 2017): 6.22, ("Ne2", 2017): 5.31, ("Ne2", 2019): 5.06,
               ("Ne2", 2021): 3.48}


@pytest.mark.parametrize("key,total", EXPECTED_IN.items())
def test_transcribed_totals(key, total):
    assert sum(a for _, a, _ in W.SEASONS[key]["events"]) == pytest.approx(total, abs=0.01)


def test_partial_passes_never_exceed_a_full_pass():
    for (site, y), s in W.SEASONS.items():
        for d, a, k in s["events"]:
            assert 0 < a <= 1.35, (site, y, d)
            dt.date.fromisoformat(f"{y}-{d}")
            assert k in ("N", "W")


def test_energy_conversion():
    e = W.energy(25.4)                          # one acre-inch per acre
    assert e["m3_ha"] == pytest.approx(254)
    assert e["diesel_l_ha"] == pytest.approx(1.88 * 1.25 / 0.404686 * 3.78541, rel=1e-6)


def test_results_respect_the_pivot():
    p = ROOT / "ml" / "results" / "mead" / "water.json"
    if not p.exists():
        pytest.skip("run ml/mead/water.py")
    for r in json.loads(p.read_text())["seasons"]:
        for pol in ("twin", "twin_blind"):
            ds = [dt.date.fromisoformat(d) for d in r[pol]["dates"]]
            assert all((b - a).days >= 4 for a, b in zip(ds, ds[1:])), (r["site"], r["year"], pol)
        assert r["rainfed"]["yield_t_ha"] <= r["potential"]["yield_t_ha"] + 1e-6
