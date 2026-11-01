"""The sub-part loader: each step of a job reports its own progress, elapsed time and,
where it can be measured, items done, data, download rate and time left."""
import datetime as dt
from types import SimpleNamespace

from fastapi.testclient import TestClient
from shapely.geometry import box

import fa_farm.sources.satellite as S
from fa_farm.sources.base import Cache
from test_console_core import _mgr, _settings
from test_console_farms import _create, app  # noqa: F401  (fixture)


def test_job_detail_is_stored_and_streamed(tmp_path):
    st, jm = _mgr(tmp_path)

    def fn(job, ctx):
        ctx.progress(0.3, "satellite", {"step": "satellite", "done": 3, "total": 12, "unit": "scenes"})
        seen.append(st.get_job(job["id"])["detail"])
        return {}
    seen = []
    jm.register("d", fn)
    j = jm.submit("d", None, {})
    jm.run_pending()
    assert seen[0]["done"] == 3 and seen[0]["total"] == 12 and seen[0]["unit"] == "scenes"


def test_each_build_step_restarts_the_sub_loader(app, monkeypatch):
    calls = []

    def builder(template, year, cache, progress=None, **kw):
        from test_console_farms import fake_builder
        progress(0.1, "soil (SoilGrids 2.0)")
        progress(0.2, "soil (SoilGrids 2.0)", {"done": 4, "total": 16, "unit": "samples"})
        progress(0.5, "satellite cover of this field (Sentinel-2)", {"done": 2, "total": 10, "unit": "scenes"})
        return fake_builder(template, year, cache, **kw)
    app.state.farms.builder = builder
    orig = app.state.store.update_job

    def spy(jid, **f):
        if "detail" in f:
            calls.append(f["detail"])
        return orig(jid, **f)
    monkeypatch.setattr(app.state.store, "update_job", spy)
    with TestClient(app) as c:
        _create(c, app)
    steps = [d["step"] for d in calls if d]
    assert "soil (SoilGrids 2.0)" in steps and "satellite cover of this field (Sentinel-2)" in steps
    soil = [d for d in calls if d and d["step"].startswith("soil")]
    sat = [d for d in calls if d and d["step"].startswith("satellite")]
    assert soil[0]["started"] == soil[-1]["started"]            # same step: same start
    assert sat[0]["started"] >= soil[0]["started"] and sat[0]["done"] == 2 and sat[0]["total"] == 10


def test_satellite_reports_scenes_data_rate_and_eta(monkeypatch, tmp_path):
    start = dt.date(2025, 4, 1)
    items = [SimpleNamespace(id=f"s{k}", datetime=dt.datetime.combine(start + dt.timedelta(days=10 * k), dt.time(10)),
                             properties={"eo:cloud_cover": 5.0}) for k in range(6)]
    monkeypatch.setattr(S, "_search", lambda *a: items)
    monkeypatch.setattr(S, "_mpc_search", lambda *a: (_ for _ in ()).throw(S.DataUnavailable("mpc", "off")))
    monkeypatch.setattr(S, "_field_ndvi", lambda it, poly: 0.4)
    got = []
    S.s2_cover_series(box(5.62, 52.52, 5.6215, 52.521), start, start + dt.timedelta(days=59), Cache(tmp_path),
                      progress=lambda f, m, d=None: got.append(d), workers=1)
    last = [d for d in got if d and d.get("done")][-1]
    assert last["done"] == 6 and last["total"] == 6 and last["unit"] == "scenes"
    assert last["bytes"] == 6 * S.SCENE_BYTES and last["approx"] is True
    assert "rate_bps" in last and "eta_s" in last


def test_planetary_computer_reports_real_bytes(monkeypatch, tmp_path):
    start = dt.date(2025, 4, 1)
    items = [SimpleNamespace(id=f"s{k}", datetime=dt.datetime.combine(start + dt.timedelta(days=3 * k), dt.time(10)),
                             properties={"eo:cloud_cover": 5.0}) for k in range(8)]
    monkeypatch.setattr(S, "_mpc_search", lambda *a: items)
    monkeypatch.setattr(S, "_field_ndvi_mpc", lambda it, poly: (0.5, 6000))
    got = []
    ser, prov = S.s2_cover_series(box(5.62, 52.52, 5.6215, 52.521), start, start + dt.timedelta(days=30),
                                  Cache(tmp_path), progress=lambda f, m, d=None: got.append(d))
    last = [d for d in got if d and d.get("done")][-1]
    assert len(ser) == 8 and last["total"] == 8 and last["bytes"] == 8 * 6000 and last["approx"] is False
    assert "planetarycomputer" in prov.url
