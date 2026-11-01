"""Fixed season cases for the SeasonSession refactor: the golden values were recorded
from ``run_season`` BEFORE its loop moved into ``SeasonSession`` (2026-11-01), and
re-recorded the same day after one deliberate model change: the weed detector's
recall and false positives now come from the measured H200 GSD sweep. Only the twin
cases moved (blight_twin: 4 -> 5 fungicide sprays, net -52/ha); calendar cases did not.

    python tests/session_cases.py     # regenerate tests/fixtures/session_golden.json
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "fixtures" / "session_golden.json"
CASES = [("blight_twin", True, "twin"), ("blight_calendar", True, "calendar"),
         ("clean_twin", False, "twin"), ("clean_calendar", False, "calendar")]


def fingerprint(res) -> dict:
    d = res.summary()
    d["missions"] = [(m["day"], m["product"], m["litres"], m["time_min"]) for m in res.missions_log]
    d["n_decisions"] = len(res.decisions)
    d["as_applied_last_sum"] = None if res.as_applied_last is None else round(sum(res.as_applied_last), 3)
    return json.loads(json.dumps(d, default=str))


def run_case(inoculum: bool, policy: str):
    from fa_drone.spec import load_spec
    from fa_runner.season import run_season
    from test_runner import _feed, _vf
    vf = _vf(inoculum=inoculum)
    return run_season(vf, policy, seed=0, feed=_feed(vf), spec=load_spec(), plan_members=6)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    import conftest  # noqa: F401  (sys.path for the services)
    out = {name: fingerprint(run_case(inoc, pol)) for name, inoc, pol in CASES}
    GOLDEN.write_text(json.dumps(out, indent=1))
    print("wrote", GOLDEN, {k: (v["net_ha"], v["fungicide_sprays"], v["herbicide"]) for k, v in out.items()})
