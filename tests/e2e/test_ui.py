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
