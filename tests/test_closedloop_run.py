"""A spray job flown as a closed loop: sortie by sortie, the sprayer's camera feeds the twin,
the rest of the job is re-planned, and a spot valve can only ever reduce product."""
import numpy as np
import pytest

from closedloop_cases import clone, session_on_flyable_day
from fa_runner.closedloop.run import fly_closed_loop


@pytest.fixture(scope="module")
def base():
    return session_on_flyable_day()


@pytest.fixture
def case(base):
    s, op = base
    c = clone(s)
    return c, c.ops[op.id]


def test_closed_loop_flies_sortie_by_sortie_and_books_the_result(case):
    s, op = case
    litres0 = s.st["litres"]                                 # the season may have flown jobs already
    out = s.fly_closed_loop(op.id)
    cl = out["closed_loop"]
    assert out["flown"] and op.status == "flown" and len(cl["sorties"]) >= 1
    assert s.st["litres"] - litres0 == pytest.approx(sum(r["litres"] for r in cl["sorties"]), abs=0.01)
    assert s.dose_cum["herbicide"].sum() > 0 and s.missions_log[-1]["product"] == "herbicide"
    assert all(r["cells_seen"] > 0 for r in cl["sorties"])


def test_closed_loop_corrects_toward_what_the_camera_saw(case):
    """Cells the camera found weedy end up treated; cells it found clean are not added."""
    s, op = case
    res = fly_closed_loop(s, op)
    truth = s.world.truth_state().weeds_m2
    thr = s.twin.field.herbicide.threshold_m2
    treated = res.dose_l_ha >= 0.9 * s.flight.rate_l_ha
    seen = np.zeros(s.grid.n_cells, bool)
    for r in res.sorties:
        seen[r.seen_cells] = True
    weedy_seen = seen & (truth > 3 * thr)
    assert weedy_seen.any() and treated[weedy_seen].mean() > 0.8


def test_valve_never_exceeds_the_plan_per_cell(case):
    s, op = case
    open_res = fly_closed_loop(clone(s), op, valve=False)
    res = fly_closed_loop(s, op, valve=True)
    assert res.valve and np.all(res.dose_l_ha <= open_res.dose_l_ha * 1.0001 + 1e-9)
    assert sum(r.litres for r in res.sorties) <= sum(r.litres for r in open_res.sorties) + 1e-6


def test_stops_when_nothing_left(case, monkeypatch):
    s, op = case
    monkeypatch.setattr(s.twin, "herbicide_targets", lambda w: ("none", np.zeros(len(w), bool)))
    res = fly_closed_loop(s, op)
    assert res.stopped == "done" and len(res.sorties) == 1


def test_every_correction_is_audited(case):
    s, op = case
    res = fly_closed_loop(s, op)
    assert res.audit and all({"sortie", "channel", "correction", "reason"} <= set(a) for a in res.audit)


def test_listener_hears_every_sortie(case):
    s, op = case
    heard = []
    s.closed_loop_listener = heard.append
    out = s.fly_closed_loop(op.id)
    assert len(heard) == len(out["closed_loop"]["sorties"]) and heard[0]["n"] == 1


def test_restore_replays_the_same_closed_loop(case):
    s, op = case
    s.fly_closed_loop(op.id, valve=True)
    r = clone(s)
    assert r.st["litres"] == pytest.approx(s.st["litres"])
    assert np.allclose(r.dose_cum["herbicide"], s.dose_cum["herbicide"])


def test_windy_day_is_not_flown(case):
    s, op = case
    s.world.weather = lambda d, _w=s.world.weather: {**_w(d), "WIND": 9.0}
    out = s.fly_closed_loop(op.id)
    assert not out["flown"] and "spray window" in out["reason"] and op.status in ("proposed", "approved")


def test_live_field_refuses_simulated_closed_loop(case):
    s, op = case
    s.world.has_truth = False
    with pytest.raises(ValueError, match="live field"):
        s.fly_closed_loop(op.id)


def test_only_underdosed_target_cells_are_flown_again(case):
    """After the first sortie the plan re-flies exactly the target cells under 90 % of the rate
    (skipped strips); cells that got their dose are never flown again, and the first sortie
    deposits what the open-loop flight deposits (no dose lost to bookkeeping)."""
    s, op = case
    open_s = clone(s)
    dose0 = s.dose_cum["herbicide"].copy()
    open_s.fly_now(op.id)                                    # same weather draw and seed as sortie 1
    cl = s.fly_closed_loop(op.id)["closed_loop"]
    first = cl["sorties"][0]
    rate = s.flight.rate_l_ha
    assert first["litres"] == pytest.approx(open_s.missions_log[-1]["litres"], rel=0.02)
    tg = np.zeros(s.grid.n_cells, bool)
    tg[cl["prediction"]["targets"]] = True
    open_dose = open_s.dose_cum["herbicide"] - dose0
    done = set(np.where(tg & (open_dose >= 0.9 * rate))[0].tolist())
    assert not set(first["targets_next"]) & done


def test_a_cell_is_tried_at_most_twice(case, monkeypatch):
    """A cell the boom cannot bring to 90 % (here: every dose is halved) is dropped after two
    passes and audited, instead of being re-flown until max_sorties."""
    import fa_runner.closedloop.run as R
    real = R.simulate_mission

    def weak(*a, **k):
        r = real(*a, **k)
        r.dose_l_ha = r.dose_l_ha * 0.5
        return r
    monkeypatch.setattr(R, "simulate_mission", weak)
    s, op = case
    res = fly_closed_loop(s, op)
    assert len(res.sorties) <= 3 and res.stopped == "done"
    assert any("2 passes" in a["reason"] for a in res.audit)
