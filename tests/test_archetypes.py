"""Task 4: archetype farms and region packs (global)."""
import datetime as dt

import pytest

from fa_farm.archetypes import load_archetypes, season_dates
from fa_farm.regions import load_region, list_regions


def test_all_eight_archetypes_load():
    a = load_archetypes()
    assert sorted(a) == ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"]
    for x in a.values():
        assert x.locations and x.field_ha[0] < x.field_ha[1] and x.sources
        assert x.region in list_regions()


def test_unsupported_archetypes_say_why():
    a = load_archetypes()
    assert a["A7"].supported is False and "tree" in a["A7"].unsupported_reason.lower()
    assert a["A8"].supported is False and a["A8"].unsupported_reason
    assert all(a[k].supported for k in ("A1", "A2", "A3", "A4", "A5", "A6"))


def test_varieties_exist_in_shipped_parameters():
    import yaml
    from fa_twin.engine import VENDOR_ROOT
    for x in load_archetypes().values():
        if not x.supported:
            continue
        d = yaml.safe_load(open(VENDOR_ROOT / "wofost72" / f"{x.crop}.yaml"))
        assert x.variety in d["CropParameters"]["Varieties"], (x.id, x.variety)


def test_southern_hemisphere_season_crosses_new_year():
    """Review focus 3: Brazil soy sown in October is harvested the next year."""
    sow, harvest = season_dates(load_archetypes()["A3"], 2024)
    assert sow == dt.date(2024, 10, 1) and harvest.year == 2025 and harvest > sow


def test_northern_season_same_year():
    sow, harvest = season_dates(load_archetypes()["A5"], 2025)
    assert sow.year == harvest.year == 2025 and harvest > sow


def test_region_rules():
    assert load_region("BR").rules.buffer_m == 20
    assert load_region("EU").rules.aerial_default_allowed is False
    assert load_region("US").rules.weight_threshold_kg == pytest.approx(24.9)
    assert load_region("US").rules.max_speed_ms == pytest.approx(13.4, abs=0.1)
    for rid in ("JP", "CN", "KR", "AU", "CA"):
        assert load_region(rid).rules.verified is False
    assert load_region("US").rules.verified and load_region("BR").rules.verified


def test_region_economics_have_currency_and_assumption_flags():
    us = load_region("US")
    assert us.currency == "USD" and us.costs["drone_service_ha"] > 0
    assert "maize" in us.prices
    assert us.prices_assumed is True       # crop prices are illustrative until set locally


def test_unknown_region_falls_back_to_global_with_warning():
    g = load_region("ZZ")
    assert g.id == "GLOBAL" and g.rules.verified is False
