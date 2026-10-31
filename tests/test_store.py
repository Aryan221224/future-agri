"""Embedded store: must work with no server, no root, and no internet."""
import datetime as dt

import pytest
from shapely.geometry import Polygon

from fa_core.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "t.duckdb") as s:
        s.add_parcel("p1", "Plot", "fa_x", Polygon([(0, 0), (20, 0), (20, 15), (0, 15)]),
                     0.03, "potato", "Potato_701")
        yield s


def test_works_without_the_spatial_extension(store):
    """The extension downloads at runtime and fails on an offline compute node."""
    g = store.parcel_geom("p1")
    assert g is not None and abs(g.area - 300.0) < 1e-6


def test_observation_ingest_is_idempotent(store):
    now = dt.datetime(2026, 11, 20, 12, 0)
    for _ in range(3):
        store.add_observation(now, "canopy_cover", 0.42, "k1", subplot_id="sp1")
    assert int(store.df("SELECT count(*) n FROM observation").n[0]) == 1


def test_different_keys_are_separate_rows(store):
    now = dt.datetime(2026, 11, 20, 12, 0)
    store.add_observation(now, "canopy_cover", 0.4, "k1", subplot_id="sp1")
    store.add_observation(now, "canopy_cover", 0.5, "k2", subplot_id="sp2")
    assert int(store.df("SELECT count(*) n FROM observation").n[0]) == 2


def test_treatment_summary_groups_by_treatment(store):
    now = dt.datetime(2026, 11, 20, 12, 0)
    for i, tr in enumerate(["T1", "T1", "T2", "T2"]):
        store.add_subplot(f"sp{i}", "p1", f"{tr}-{i}", tr, i, None, 20.0)
        store.add_observation(now, "canopy_cover", 0.3 + 0.2 * (tr == "T2"),
                              f"k{i}", subplot_id=f"sp{i}")
    df = store.treatment_summary()
    assert set(df.treatment) == {"T1", "T2"}
    assert df[df.treatment == "T2"].mean_cover.iloc[0] > df[df.treatment == "T1"].mean_cover.iloc[0]


def test_review_queue_only_returns_unreviewed(store):
    now = dt.datetime(2026, 11, 20, 12, 0)
    store.add_detection("d1", now, "weed", needs_review=True)
    store.add_detection("d2", now, "crop", needs_review=False)
    assert len(store.review_queue()) == 1


def test_phenology_upsert_keeps_one_row_per_subplot_visit(store):
    now = dt.datetime(2026, 11, 20, 12, 0)
    store.add_phenology(now, "sp1", stage=41)
    store.add_phenology(now, "sp1", stage=43)      # corrected in the field
    df = store.df("SELECT stage FROM phenology")
    assert len(df) == 1 and df.stage.iloc[0] == 43
