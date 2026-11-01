"""Live message schema fa.live/1: what the on-board agent sends, checked item by item."""
import math

import pytest

from fa_core.live_msgs import SCHEMA, validate_batch


def _tel(**kw):
    return {"t": "2025-06-05T07:00:00Z", "lat": 52.52, "lon": 5.62, "alt_agl_m": 2.5, "heading_deg": 90.0,
            "speed_ms": 5.0, "spraying": True, "sortie": 1, **kw}


def _cell(i, **kw):
    return {"t": "2025-06-05T07:00:01Z", "cell": i, "weeds": 3, "area_m2": 100.0, "gsd_m": 0.002,
            "blight": False, **kw}


def _batch(telemetry=(), cells=(), **kw):
    return {"schema": SCHEMA, "run_id": "run_x", "seq": 1, "sent_at": "2025-06-05T07:00:02Z",
            "telemetry": list(telemetry), "cells": list(cells), **kw}


def test_good_batch_passes_whole():
    ok, errs = validate_batch(_batch([_tel()], [_cell(0), _cell(1, cc=0.4)]), n_cells=10)
    assert not errs and len(ok["telemetry"]) == 1 and len(ok["cells"]) == 2


def test_bad_items_rejected_individually():
    b = _batch(telemetry=[_tel(), _tel(lat=float("nan"))], cells=[_cell(5), _cell(10_000), _cell(3, weeds=-1)])
    ok, errs = validate_batch(b, n_cells=100)
    assert len(ok["telemetry"]) == 1 and [c["cell"] for c in ok["cells"]] == [5]
    assert len(errs) == 3 and any("telemetry 1" in e and "lat" in e for e in errs)
    assert any("cell 1" in e for e in errs) and any("weeds" in e for e in errs)


@pytest.mark.parametrize("bad", [{"lon": 200.0}, {"alt_agl_m": -5}, {"sortie": 0}, {"spraying": "yes"},
                                 {"speed_ms": math.inf}])
def test_telemetry_ranges(bad):
    ok, errs = validate_batch(_batch([_tel(**bad)]), 10)
    assert not ok["telemetry"] and len(errs) == 1


@pytest.mark.parametrize("bad", [{"area_m2": 0}, {"gsd_m": -1}, {"cc": 1.5}, {"weeds": 2.5}, {"cell": "3"}])
def test_cell_ranges(bad):
    ok, errs = validate_batch(_batch(cells=[_cell(3, **bad)]), 10)
    assert not ok["cells"] and len(errs) == 1


@pytest.mark.parametrize("env", [{"schema": "x"}, {"run_id": ""}, {"seq": -1}, {"seq": "1"},
                                 {"telemetry": "no"}])
def test_bad_envelope_raises(env):
    with pytest.raises(ValueError):
        validate_batch({**_batch(), **env}, 100)


def test_oversized_batch_is_refused():
    with pytest.raises(ValueError, match="at most"):
        validate_batch(_batch(cells=[_cell(0)] * 20_001), 10)
