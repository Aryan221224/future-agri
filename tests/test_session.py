"""SeasonSession: the closed loop as a stepper (auto mode == run_season; manual approvals)."""
import json

import pytest

from fa_drone.spec import load_spec
from fa_runner.season import run_season
from fa_runner.session import SeasonSession
from session_cases import CASES, GOLDEN, fingerprint
from test_runner import _feed, _vf

SPEC = load_spec()
GOLD = json.loads(GOLDEN.read_text())


def _kwargs(inoculum=True, policy="twin", approvals="manual"):
    vf = _vf(inoculum=inoculum)
    return dict(vf=vf, policy=policy, seed=0, feed=_feed(vf), spec=SPEC, plan_members=6,
                approvals=approvals)


def _session(**kw):
    return SeasonSession(**_kwargs(**kw))


@pytest.mark.parametrize("name,inoculum,policy", CASES)
def test_auto_session_matches_golden_run_season(name, inoculum, policy):
    s = _session(inoculum=inoculum, policy=policy, approvals="auto")
    s.run_until()
    assert s.done
    assert fingerprint(s.finish()) == GOLD[name]


def test_run_season_wrapper_still_matches_golden():
    vf = _vf(inoculum=False)
    r = run_season(vf, "calendar", seed=0, feed=_feed(vf), spec=SPEC, plan_members=6)
    assert fingerprint(r) == GOLD["clean_calendar"]


def test_manual_mode_stops_on_first_proposal():
    s = _session()
    s.run_until(stop_on_decision=True)
    props = [o for o in s.ops.values() if o.status == "proposed"]
    assert props and not s.done
    assert s.day < s.world.harvest
    assert s.phase == "flights"           # paused after the morning decisions, before flying


def test_approving_everything_equals_auto():
    s = _session()
    while not s.done:
        s.run_until(stop_on_decision=True)
        for o in list(s.ops.values()):
            if o.status == "proposed":
                s.approve(o.id)
    assert fingerprint(s.finish()) == GOLD["blight_twin"]


def test_rejected_herbicide_is_never_flown():
    s = _session(inoculum=False, policy="calendar")
    seen = False
    while not s.done:
        s.run_until(stop_on_decision=True)
        for o in list(s.ops.values()):
            if o.status == "proposed":
                if o.product == "herbicide":
                    s.reject(o.id, "operator: weeds below threshold")
                    seen = True
                else:
                    s.approve(o.id)
    res = s.finish()
    assert seen
    assert not any(m["product"] == "herbicide" for m in res.missions_log)
    assert any(e["kind"] == "rejected" for e in s.events)


def test_defer_moves_due_date():
    s = _session(inoculum=False, policy="calendar")
    s.run_until(stop_on_decision=True)
    op = next(o for o in s.ops.values() if o.status == "proposed")
    old = op.due
    s.defer(op.id, 3)
    assert s.ops[op.id].due == old + __import__("datetime").timedelta(days=3)
    assert s.ops[op.id].status == "proposed"


def test_trace_has_one_row_per_day_and_snapshots():
    s = _session(inoculum=True, approvals="auto")
    s.run_until()
    w = s.world
    assert len(s.trace) == (w.harvest - w.sowing).days
    row = s.trace[40]
    for k in ("day", "das", "tmax", "tmin", "rain_mm", "wind_ms", "truth_lai", "truth_cc", "truth_dvs",
              "truth_weeds_m2", "truth_blight", "twin_lai", "twin_dvs", "twin_twso"):
        assert k in row, k
    assert len(s.snapshots) >= (w.harvest - w.sowing).days // 7
    snap = next(iter(s.snapshots.values()))
    assert set(snap) == {"lai", "cc", "weeds", "blight", "dose_fungicide", "dose_herbicide"}
    assert len(snap["lai"]) == w.grid.n_cells


def test_restore_replays_actions_to_same_state():
    s = _session()
    s.run_until(stop_on_decision=True)
    first = next(o for o in s.ops.values() if o.status == "proposed")
    s.approve(first.id)
    s.run_until(stop_on_decision=True)
    s.step()
    r = SeasonSession.restore(_kwargs(), s.actions)
    assert r.state() == s.state()
    assert json.dumps(s.state(), default=str)          # JSON-able


def test_twin_decisions_speak_the_farm_currency():
    s = _session(inoculum=True, approvals="auto")          # an EU (EUR) farm
    s.run_until(day=s.world.sowing + __import__("datetime").timedelta(days=50))
    plans = [e["text"] for e in s.events if e["kind"] == "plan"]
    assert plans
    assert all("Rs" not in t for t in plans), plans[:2]
    assert any("EUR" in t for t in plans if "gain" in t)
