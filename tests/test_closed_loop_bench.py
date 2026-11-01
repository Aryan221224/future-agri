"""The closed-loop benchmark: open vs closed vs closed + valve on the same job, paired."""
import json

import pytest

from closedloop_cases import herb_kwargs


def _factory(template_id):
    return herb_kwargs()["vf"]


def test_bench_arms_on_the_same_job(tmp_path):
    from closed_loop_bench import run_bench
    r = run_bench(["synthetic"], [0], tmp_path, workers=1, farm_factory=_factory, finish=False)
    rows = r["rows"]
    assert {x["arm"] for x in rows} == {"open", "closed", "closed_valve"}
    assert len({x["op_id"] for x in rows}) == 1 and len({x["day"] for x in rows}) == 1   # the same job, same day
    for x in rows:
        assert x["litres"] > 0 and x["sorties"] >= 1 and x["weeds_after_m2"] >= 0
    s = r["summary"]
    assert set(s) == {"open", "closed", "closed_valve", "by_template"}
    assert {"litres_diff_mean", "litres_diff_ci95", "weeds_after_diff_mean", "litres_pct_diff_mean", "n"} <= set(s["closed"])
    assert json.loads((tmp_path / "closed_loop.json").read_text())["summary"]["closed"]["n"] == 1
    assert "| synthetic | closed |" in (tmp_path / "closed_loop.md").read_text()


@pytest.mark.slow
def test_bench_runs_and_reports_paired_differences(tmp_path):
    from closed_loop_bench import run_bench
    r = run_bench(["synthetic"], [0, 1], tmp_path, workers=2, farm_factory=_factory)
    arms = r["summary"]
    for a in ("closed", "closed_valve"):
        assert {"litres_diff_mean", "litres_diff_ci95", "net_pct_diff_mean", "weeds_after_diff_mean"} <= set(arms[a])
        assert "net_diff_mean" in arms["by_template"]["synthetic"][a]
        assert arms[a]["n"] == 2
    assert (tmp_path / "closed_loop.md").exists()


def test_a_template_that_cannot_be_simulated_is_skipped_not_fatal(tmp_path):
    from closed_loop_bench import run_bench

    def factory(t):
        if t == "broken":
            raise ValueError("archetype broken not supported")
        return _factory(t)
    r = run_bench(["broken", "synthetic"], [0], tmp_path, workers=1, farm_factory=factory, finish=False)
    skipped = [x for x in r["rows"] if x.get("skipped")]
    assert len(skipped) == 1 and "not supported" in skipped[0]["skipped"] and skipped[0]["template"] == "broken"
    assert r["summary"]["closed"]["n"] == 1


def test_money_is_never_pooled_across_currencies():
    """Farms in different currencies: absolute differences are reported per template; across
    templates only relative (%) differences are pooled."""
    from closed_loop_bench import summarise
    rows = []
    for t, cur, net, litres in (("A1", "JPY", 1_500_000.0, 6.0), ("A5", "EUR", 7000.0, 60.0)):
        for seed in (0, 1):
            rows.append({"template": t, "seed": seed, "arm": "open", "litres": litres, "sorties": 1, "time_min": 5.0,
                         "weeds_after_m2": 1.0, "yield_t_ha": 10.0, "net_ha": net, "currency": cur})
            rows.append({"template": t, "seed": seed, "arm": "closed", "litres": litres * 1.2, "sorties": 2,
                         "time_min": 6.0, "weeds_after_m2": 0.8, "yield_t_ha": 10.0, "net_ha": net * 0.99, "currency": cur})
    s = summarise(rows)
    assert s["by_template"]["A1"]["closed"]["net_diff_mean"] == pytest.approx(-15_000.0)
    assert s["by_template"]["A1"]["currency"] == "JPY" and s["by_template"]["A5"]["currency"] == "EUR"
    assert "net_ha_mean" not in s["closed"] and "net_diff_mean" not in s["closed"]
    assert s["closed"]["litres_pct_diff_mean"] == pytest.approx(20.0)
    assert s["closed"]["net_pct_diff_mean"] == pytest.approx(-1.0)
    assert s["closed"]["weeds_after_pct_diff_mean"] == pytest.approx(-20.0)
