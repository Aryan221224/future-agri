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
