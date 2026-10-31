"""Task 1: provenance, offline cache, named failures."""
import pytest
import requests

from fa_farm.sources.base import Cache, DataUnavailable, Provenance, fetch_json


def test_cache_roundtrip(tmp_path):
    c = Cache(tmp_path)
    k = c.key("power", 52.5, 5.6, "2025-01-01")
    assert c.get_json(k) is None
    c.put_json(k, {"a": 1})
    assert c.get_json(k) == {"a": 1}
    c.put_bytes(k + ".bin", b"xyz")
    assert c.get_bytes(k + ".bin") == b"xyz"


def test_provenance_dict():
    p = Provenance("NASA POWER", "https://power.larc.nasa.gov", "open", "2026-10-31", "NASA LaRC POWER")
    d = p.as_dict()
    assert d["source"] == "NASA POWER" and d["commercial_use"] is True


def test_fetch_json_uses_cache_offline(tmp_path, monkeypatch):
    c = Cache(tmp_path)
    url, params = "https://example.org/x", {"q": 1}
    c.put_json(c.key(url, params), {"cached": True})

    def boom(*a, **k):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(requests.Session, "get", boom)
    assert fetch_json(url, params, c, layer="weather") == {"cached": True}


def test_fetch_json_missing_names_layer(tmp_path, monkeypatch):
    """Review focus 5: offline with no cache -> the failure names the layer."""
    def boom(*a, **k):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(requests.Session, "get", boom)
    with pytest.raises(DataUnavailable) as e:
        fetch_json("https://example.org/y", {}, Cache(tmp_path), layer="soil")
    assert e.value.layer == "soil"
    assert "soil" in str(e.value)
