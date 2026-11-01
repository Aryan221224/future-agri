"""End-to-end: the console in a real browser."""
import json

import pytest

from conftest import shot, square

pytestmark = pytest.mark.e2e


def test_create_farm_wizard_builds(page, console, tmp_path):
    base = console["base"]
    gj = tmp_path / "field.geojson"
    gj.write_text(json.dumps({"type": "Feature", "geometry": square(), "properties": {}}))
    page.goto(base + "/#/farms")
    page.get_by_role("link", name="Add a farm").first.click()
    page.get_by_role("button", name="Draw or upload").click()
    page.set_input_files("[data-testid=boundary-file]", str(gj))
    page.get_by_text("Field boundary set from field.geojson.").wait_for()
    shot(page, "01_new_farm_where")
    page.get_by_role("button", name="Continue").click()
    page.get_by_label("Season", exact=True).select_option("2025")
    page.get_by_label("Farm name", exact=True).fill("North field")
    shot(page, "02_new_farm_what")
    page.get_by_role("button", name="Build the digital farm").click()
    page.wait_for_url("**/#/f/*/mission", timeout=60_000)
    page.goto(base + "/#/farms")
    row = page.locator(".row", has_text="North field")
    row.get_by_text("ready").wait_for(timeout=20_000)
    shot(page, "03_farms_list")
    assert not page.errors, page.errors


def _run_session(console, fid, until):
    import json as _j
    import time as _t
    import urllib.request as _u
    body = _j.dumps({"until": until, "stop_on_decision": False, "approve_all": True}).encode()
    req = _u.Request(console["base"] + f"/api/farms/{fid}/session/run", data=body,
                     headers={"Content-Type": "application/json"})
    jid = _j.loads(_u.urlopen(req).read())["id"]
    for _ in range(600):
        j = _j.loads(_u.urlopen(console["base"] + f"/api/jobs/{jid}").read())
        if j["status"] in ("done", "failed"):
            break
        _t.sleep(0.25)
    assert j["status"] == "done", j


def _canvas_variance(page, selector="canvas") -> float:
    import io

    from PIL import Image, ImageStat
    png = page.locator(selector).first.screenshot()
    st = ImageStat.Stat(Image.open(io.BytesIO(png)).convert("L"))
    return st.stddev[0]


def test_twin3d_renders_and_picks(page, console):
    from conftest import make_farm
    fid = make_farm(console, "3D field")
    _run_session(console, fid, "2025-06-25")
    page.goto(console["base"] + f"/#/f/{fid}/twin")
    page.locator("canvas").first.wait_for(timeout=30_000)
    page.get_by_text("Layer").first.wait_for()
    page.wait_for_timeout(1500)
    assert _canvas_variance(page) > 8           # a rendered scene, not a blank canvas
    page.get_by_role("radio", name="Weeds").check()
    box = page.locator("canvas").first.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.get_by_text("Cell", exact=False).first.wait_for(timeout=5000)
    shot(page, "04_twin3d")
    assert not page.errors, page.errors


def test_drone_playback(page, console):
    from conftest import make_farm
    fid = make_farm(console, "Drone field")
    page.goto(console["base"] + f"/#/f/{fid}/drone")
    page.get_by_text("JIYI K3-A Pro").first.wait_for(timeout=30_000)
    page.locator("canvas").first.wait_for()
    page.wait_for_timeout(800)
    assert _canvas_variance(page) > 5                       # the aircraft is drawn
    shot(page, "05_drone_studio")
    page.get_by_label("Speed (m/s)").fill("6")
    page.get_by_role("button", name="Simulate mission").click()
    page.get_by_text("Sorties").first.wait_for(timeout=30_000)
    elapsed = page.get_by_test_id("gauge-elapsed")
    before = elapsed.inner_text()
    page.get_by_role("button", name="Play the flight").click()
    page.wait_for_timeout(2500)
    assert elapsed.inner_text() != before
    sorties = int(page.get_by_test_id("kpi-sorties").inner_text())
    assert sorties >= 1
    shot(page, "06_drone_playback")
    assert not page.errors, page.errors


def test_mission_step_approve_fly(page, console):
    from conftest import make_farm
    fid = make_farm(console, "Mission field")
    page.goto(console["base"] + f"/#/f/{fid}/mission")
    page.get_by_role("heading", name="Mission").wait_for(timeout=30_000)
    page.get_by_role("button", name="Next day").click()
    page.get_by_text("day 2 of", exact=False).first.wait_for(timeout=20_000)
    page.get_by_role("button", name="Run to the next decision").click()
    approve = page.get_by_role("button", name="Approve").first
    approve.wait_for(timeout=90_000)
    shot(page, "07_mission_decision")
    approve.click()
    page.get_by_text("approved", exact=False).first.wait_for(timeout=10_000)
    page.get_by_role("button", name="Run 7 days").click()
    page.locator(".timeline li").first.wait_for(timeout=90_000)
    page.wait_for_timeout(1500)
    text = page.locator(".timeline").inner_text()
    assert "approved" in text and ("flown" in text or "spray window" in text or "dropped" in text or "proposed" in text)
    shot(page, "08_mission_after_flight")
    assert not page.errors, page.errors


def test_season_charts_render(page, console):
    from conftest import make_farm
    fid = make_farm(console, "Season field")
    _run_session(console, fid, "2025-07-15")
    page.goto(console["base"] + f"/#/f/{fid}/season")
    page.get_by_role("heading", name="Season").wait_for(timeout=30_000)
    page.locator("canvas").nth(2).wait_for(timeout=20_000)
    assert page.locator("canvas").count() >= 4
    page.get_by_text("Twin estimate").first.wait_for()
    shot(page, "09_season_charts")
    assert not page.errors, page.errors


def test_iterate_to_approved_plan_exports(page, console):
    import json as _j
    from pathlib import Path as _P

    from conftest import make_farm
    fid = make_farm(console, "Plan field")
    res = _j.loads((_P(__file__).parents[1] / "fixtures" / "perfect_small.json").read_text())
    app = console["app"]
    st = app.state.store
    job = st.create_job("perfect", fid, {"seeds": 1}, target="local")
    (app.state.settings.jobs_dir / f"{job['id']}.json").write_text(_j.dumps(res))
    st.update_job(job["id"], status="done", progress=1.0, result=res["summary"])
    page.goto(console["base"] + f"/#/f/{fid}/iterate")
    page.get_by_role("heading", name="Iterations", exact=True).wait_for(timeout=30_000)
    page.get_by_text("Ranked plans").wait_for()
    shot(page, "10_iterate")
    row = page.locator("tr", has_text="Twin + blight alert")
    row.get_by_role("button", name="Use this plan").click()
    page.wait_for_url("**/plan", timeout=20_000)
    page.get_by_role("button", name="Approve this plan").click()
    page.get_by_text("Approved", exact=True).first.wait_for(timeout=20_000)
    shot(page, "11_plan")
    for path in ("plan.pdf", "plan.ics", "plan.csv", "kml", "survey.kml", "settings"):
        r = page.request.get(console["base"] + f"/api/farms/{fid}/export/{path}")
        assert r.status == 200, path
    assert not page.errors, page.errors


def test_today_is_english_with_theme_toggle(page, console):
    import re
    from conftest import make_farm
    fid = make_farm(console, "Today field")
    page.goto(console["base"] + f"/#/today/{fid}")
    page.get_by_role("heading", name="Today on Today field").wait_for(timeout=30_000)
    advice = page.get_by_test_id("advice")
    advice.wait_for()
    assert advice.inner_text().strip()
    assert page.get_by_label("Language").count() == 0                  # English only: no language picker
    assert not re.search("[\u0900-\u097F]", page.locator("body").inner_text())
    shot(page, "12_today")
    toggle = page.get_by_role("group", name="Colour theme")
    toggle.get_by_role("button", name="Light").click()
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")
    shot(page, "13_today_light")
    toggle.get_by_role("button", name="Dark").click()
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    page.set_viewport_size({"width": 390, "height": 844})      # the farmer's phone
    page.wait_for_timeout(300)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    shot(page, "13b_today_phone")
    page.get_by_role("button", name="Team").click()          # back to the team console
    page.wait_for_url("**/#/f/*/mission", timeout=10_000)
    page.get_by_role("heading", name="Mission", exact=True).wait_for(timeout=20_000)
    assert not page.errors, page.errors


def test_theme_toggle_in_header_persists(page, console):
    from conftest import make_farm
    fid = make_farm(console, "Theme field")
    page.goto(console["base"] + f"/#/f/{fid}/season")
    page.get_by_role("heading", name="Season", exact=True).wait_for(timeout=30_000)
    toggle = page.get_by_role("group", name="Colour theme")
    assert toggle.get_by_role("button", name="Dark").get_attribute("aria-pressed") == "true"
    toggle.get_by_role("button", name="Light").click()
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")
    page.get_by_role("heading", name="Season", exact=True).wait_for()   # the page redraws in the new colours
    shot(page, "18_season_light")
    page.reload()
    page.get_by_role("heading", name="Season", exact=True).wait_for(timeout=30_000)
    assert page.evaluate("document.documentElement.dataset.theme") == "light"      # remembered
    page.get_by_role("group", name="Colour theme").get_by_role("button", name="Dark").click()
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    assert console["app"].state.store.settings().get("theme") == "dark"
    assert not page.errors, page.errors


def test_live_page_handles_unavailable(page, console, monkeypatch):
    import fa_console.live as live
    from fa_farm.sources.base import DataUnavailable

    def boom(self, *a, **k):
        raise DataUnavailable("forecast", "no network in the test")
    monkeypatch.setattr(live.LiveService, "_forecast_members", boom)
    from conftest import make_farm
    fid = make_farm(console, "Live field")
    page.goto(console["base"] + f"/#/f/{fid}/live")
    page.get_by_role("heading", name="Live", exact=True).wait_for(timeout=30_000)
    page.get_by_text("Spray outlook unavailable").wait_for(timeout=20_000)
    page.get_by_text("no network in the test").first.wait_for()
    page.get_by_role("heading", name="Observed weather").wait_for()
    page.get_by_role("button", name="Add a device").click()
    page.get_by_label("Device name").fill("Probe 1")
    page.get_by_role("button", name="Create device").click()
    page.get_by_text("Copy this token now").wait_for(timeout=10_000)
    shot(page, "14_live")
    page.get_by_role("button", name="Done").click()
    page.locator("tr", has_text="Probe 1").wait_for()
    assert not page.errors, page.errors


def test_system_shows_worker_token_and_sources(page, console):
    page.goto(console["base"] + "/#/system")
    page.get_by_role("heading", name="System", exact=True).wait_for(timeout=30_000)
    tok = console["app"].state.jobs.worker_token()
    page.get_by_role("button", name="Show token").click()
    page.get_by_text(tok).first.wait_for()
    page.get_by_text("Connect-GPU-Server").first.wait_for()
    page.get_by_role("link", name="SoilGrids 2.0, ISRIC").wait_for()
    seg = page.get_by_role("group", name="Theme", exact=True)          # the System setting, not the header toggle
    seg.get_by_role("button", name="Light", exact=True).click()
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")
    seg.get_by_role("button", name="Dark", exact=True).click()
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    shot(page, "15_system")
    assert not page.errors, page.errors


def test_records_manual_entry_shows_missing_fields(page, console):
    from conftest import make_farm
    fid = make_farm(console, "Records field")
    page.goto(console["base"] + f"/#/f/{fid}/records")
    page.get_by_role("heading", name="Records", exact=True).wait_for(timeout=30_000)
    page.get_by_role("button", name="Add a record").click()
    page.get_by_label("Product").select_option("fungicide")
    page.get_by_label("Litres sprayed").fill("15")
    page.get_by_label("Area sprayed (ha)").fill("1")
    page.get_by_role("button", name="Save record").click()
    row = page.locator("tbody tr", has_text="fungicide")
    row.wait_for(timeout=10_000)
    row.get_by_text("missing", exact=False).first.wait_for()
    shot(page, "16_records")
    r = page.request.get(console["base"] + f"/api/farms/{fid}/export/records.csv")
    assert r.status == 200 and "fungicide" in r.text()
    assert not page.errors, page.errors


def test_ortho_upload_needs_gpu_server(page, console):
    import datetime as dt
    import time
    import urllib.request
    from conftest import square
    from fa_farm.archetypes import season_dates
    from test_console_session import _fake_field
    farms = console["app"].state.farms
    old = farms.field_builder, farms.today
    farms.field_builder, farms.today = _fake_field(50), (lambda: dt.date(2025, 6, 15))
    try:
        sow, _ = season_dates(farms.template("A5"), 2025)
        body = json.dumps({"name": "Ortho field", "mode": "field", "template": "A5", "polygon": square(),
                           "sowing": sow.isoformat()}).encode()
        req = urllib.request.Request(console["base"] + "/api/farms", data=body, headers={"Content-Type": "application/json"})
        fid = json.loads(urllib.request.urlopen(req).read())["farm"]["id"]
        for _ in range(300):
            if json.loads(urllib.request.urlopen(console["base"] + f"/api/farms/{fid}").read())["status"] == "ready":
                break
            time.sleep(0.2)
        page.goto(console["base"] + f"/#/f/{fid}/mission")
        page.get_by_role("heading", name="Mission", exact=True).wait_for(timeout=30_000)
        page.get_by_role("button", name="Upload a scan").click()
        page.get_by_role("button", name="Orthomosaic (GeoTIFF)").click()
        page.get_by_text("No GPU server with the weed detector is connected").wait_for(timeout=10_000)
        assert page.get_by_role("button", name="Detect weeds").is_disabled()
        shot(page, "17_ortho_upload")
        assert not page.errors, page.errors
    finally:
        farms.field_builder, farms.today = old


def test_build_shows_sub_loader_with_rate(page, console, tmp_path):
    """Under the main bar: the current step's own loader with items, data, rate and time left."""
    import threading

    from test_console_farms import fake_builder
    app = console["app"]
    release = threading.Event()

    def slow(template, year, cache, progress=None, **kw):
        progress(0.6, "satellite cover of this field (Sentinel-2)",
                 {"done": 7, "total": 25, "unit": "scenes", "bytes": 68_000, "approx": False,
                  "rate_bps": 24_000, "eta_s": 9, "label": "satellite scene 7/25: 6 clear over this field"})
        release.wait(30)
        return fake_builder(template, year, cache, **kw)
    app.state.farms.builder = slow
    try:
        gj = tmp_path / "field.geojson"
        gj.write_text(json.dumps({"type": "Feature", "geometry": square(), "properties": {}}))
        page.goto(console["base"] + "/#/farms/new")
        page.get_by_role("button", name="Draw or upload").click()
        page.set_input_files("[data-testid=boundary-file]", str(gj))
        page.get_by_role("button", name="Continue").click()
        page.get_by_label("Farm name", exact=True).fill("Slow link field")
        page.get_by_role("button", name="Build the digital farm").click()
        sub = page.get_by_test_id("subloader").first
        sub.wait_for(timeout=20_000)
        text = sub.inner_text()
        for part in ("satellite scene 7/25", "7/25 scenes", "68 kB", "24 kB/s", "about 9 s left", "on this step"):
            assert part in text, (part, text)
        shot(page, "13_build_sub_loader")
    finally:
        release.set()
        app.state.farms.builder = fake_builder
    assert not page.errors, page.errors


def _farm_with_herbicide_op(console):
    """A virtual farm paused on a flyable day with a proposed spot-herbicide job."""
    import dataclasses

    from conftest import make_farm
    from fa_runner.scenarios import flight_record
    from test_console_farms import fake_builder
    app = console["app"]

    def weedy(*a, **k):
        vf = fake_builder(*a, **k)
        vf.spec = dataclasses.replace(vf.spec, weed_mean_m2=25.0)
        return vf
    app.state.farms.builder = weedy
    try:
        fid = make_farm(console, "Closed-loop field")
    finally:
        app.state.farms.builder = fake_builder
    svc = app.state.sessions
    op = None
    for _ in range(200):
        svc.act(fid, "run_until", {"stop_on_decision": True})
        s = svc.get(fid)
        props = [o for o in s.ops.values() if o.status == "proposed"]
        op = next((o for o in props if o.product == "herbicide"), None)
        if op:
            break
        for o in props:
            svc.act(fid, "approve", {"op_id": o.id})
    for _ in range(40):
        rec = s.world.weather(s.day)
        if rec is not None and s.window.ok(flight_record(rec)):
            break
        svc.act(fid, "step", {})
    return fid, op.id


def test_closed_loop_live_view(page, console):
    fid, op = _farm_with_herbicide_op(console)
    page.goto(console["base"] + f"/#/f/{fid}/mission")
    page.get_by_role("button", name="Fly closed loop").first.click()
    page.wait_for_url("**/live-run", timeout=20_000)
    page.get_by_role("heading", name="Live run").wait_for()
    page.get_by_role("table", name="Sortie log").get_by_role("row").nth(1).wait_for(timeout=60_000)
    page.get_by_text("Job complete", exact=False).first.wait_for(timeout=60_000)
    for t in ("Planned", "Flown", "Freeze plan (open loop)", "Download next sortie (KML)"):
        assert page.get_by_text(t, exact=False).first.is_visible(), t
    page.wait_for_timeout(800)
    assert _canvas_variance(page) > 5
    shot(page, "14_live_run")
    page.get_by_role("button", name="Light").click()
    page.wait_for_timeout(500)
    shot(page, "14_live_run_light")
    assert not page.errors, page.errors
