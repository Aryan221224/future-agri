"""Sentinel-2 cover series on slow links: a bounded number of scenes (the least cloudy
per 10-day window), progress per scene, a time budget, and the old cache honoured."""
import datetime as dt
from types import SimpleNamespace

import pytest
from shapely.geometry import box

import fa_farm.sources.satellite as S
from fa_farm.sources.base import Cache

POLY = box(5.62, 52.52, 5.6215, 52.521)


def _item(day: dt.date, cloud: float, tile: str = "31UFU"):
    return SimpleNamespace(id=f"S2_{tile}_{day:%Y%m%d}", datetime=dt.datetime.combine(day, dt.time(10, 30)),
                           properties={"eo:cloud_cover": cloud})


@pytest.fixture
def fake(monkeypatch):
    start = dt.date(2025, 4, 1)
    items = []
    for k in range(60):                                 # a sunny season: a scene every 2-3 days
        d = start + dt.timedelta(days=int(k * 2.5))
        items.append(_item(d, cloud=(k * 37) % 60))
        items.append(_item(d, cloud=50.0, tile="31UGU"))  # the same day from an overlapping tile
    monkeypatch.setattr(S, "_search", lambda poly, a, b, max_cloud: list(items))
    read = []

    def ndvi(it, poly):
        read.append(it.id)
        return None if it.properties["eo:cloud_cover"] > 55 else 0.2 + 0.001 * len(read)
    monkeypatch.setattr(S, "_field_ndvi", ndvi)
    return items, read, start


def test_series_reads_at_most_one_scene_per_window(fake, tmp_path):
    items, read, start = fake
    end = start + dt.timedelta(days=150)
    seen = []
    ser, _ = S.s2_cover_series(POLY, start, end, Cache(tmp_path), progress=lambda f, m: seen.append((f, m)))
    windows = 150 // S.WINDOW_DAYS + 1
    assert len(ser) <= windows and len(ser) >= windows - 2
    assert len(read) <= 2 * windows                       # a cloudy pick gets one fallback, no more
    days = [d for d, _, _ in ser]
    assert days == sorted(days) and len(set(days)) == len(days)
    assert all(b - a >= dt.timedelta(days=1) for a, b in zip(days, days[1:]))
    assert seen and all(0 <= f <= 1 for f, _ in seen) and seen[-1][0] == pytest.approx(1.0)
    assert [f for f, _ in seen] == sorted(f for f, _ in seen)
    assert "scene" in seen[0][1]


def test_least_cloudy_scene_of_a_window_is_read_first(fake, tmp_path):
    items, read, start = fake
    S.s2_cover_series(POLY, start, start + dt.timedelta(days=9), Cache(tmp_path))
    first = next(it for it in items if it.id == read[0])
    window = [it for it in items if it.datetime.date() <= start + dt.timedelta(days=9)]
    assert first.properties["eo:cloud_cover"] == min(it.properties["eo:cloud_cover"] for it in window)


def test_time_budget_keeps_what_arrived(fake, tmp_path, monkeypatch):
    items, read, start = fake
    clock = iter(range(0, 10_000, 100))                     # every scene "takes" 100 s
    monkeypatch.setattr(S.time, "monotonic", lambda: next(clock))
    ser, prov = S.s2_cover_series(POLY, start, start + dt.timedelta(days=150), Cache(tmp_path), budget_s=450)
    assert 0 < len(ser) <= 5 and "time budget" in prov.note


def test_old_full_series_cache_is_used(fake, tmp_path):
    items, read, start = fake
    end = start + dt.timedelta(days=30)
    cache = Cache(tmp_path)
    old = cache.key("s2", POLY.wkt, start.isoformat(), end.isoformat(), 60.0)
    cache.put_json(old, [[start.isoformat(), 0.5], [(start + dt.timedelta(days=3)).isoformat(), 0.6]])
    ser, _ = S.s2_cover_series(POLY, start, end, cache)
    assert len(ser) == 2 and not read                       # shipped template caches stay offline
