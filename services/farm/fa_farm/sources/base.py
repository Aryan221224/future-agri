"""Shared plumbing for real-data adapters: provenance, an offline-first cache,
HTTP with the session proxy, and failures that name the missing layer.

Every adapter returns its data together with a :class:`Provenance` so a report
can carry the source, licence and attribution of every number it shows. The
cache is content-addressed (sha256 of the request), so a run whose inputs are
cached reproduces bit-for-bit with the network off.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

DEFAULT_CACHE = Path(os.environ.get("FA_DATA_CACHE", Path.home() / ".cache" / "futureagri" / "data"))
CA_BUNDLE = "/root/.ccr/ca-bundle.crt"


@dataclass(frozen=True)
class Provenance:
    source: str
    url: str
    licence: str
    retrieved: str
    attribution: str
    commercial_use: bool = True
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def today() -> str:
    return dt.date.today().isoformat()


class DataUnavailable(RuntimeError):
    """A real-data layer could not be fetched and is not cached.

    There is deliberately no synthetic fallback here: a virtual farm built on
    invented soil or weather while claiming real data is the failure this
    subsystem exists to prevent.
    """

    def __init__(self, layer: str, detail: str = ""):
        self.layer = layer
        super().__init__(f"real-data layer '{layer}' unavailable (not cached, fetch failed): {detail}")


class Cache:
    """Content-addressed on-disk cache (JSON and bytes)."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else DEFAULT_CACHE
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(*parts) -> str:
        blob = json.dumps(parts, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:32]

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / key

    def get_json(self, key: str):
        p = self._path(key + ".json")
        if not p.exists():
            return None
        return json.loads(p.read_text())

    def put_json(self, key: str, obj) -> None:
        p = self._path(key + ".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, default=str))
        tmp.replace(p)

    def get_bytes(self, key: str) -> bytes | None:
        p = self._path(key)
        return p.read_bytes() if p.exists() else None

    def put_bytes(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)


_SESSION: requests.Session | None = None


def session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers["User-Agent"] = "FutureAgri/0.3 (+digital twin research)"
        if os.path.exists(CA_BUNDLE) and not os.environ.get("REQUESTS_CA_BUNDLE"):
            s.verify = CA_BUNDLE
        _SESSION = s
    return _SESSION


def fetch_json(url: str, params: dict | None, cache: Cache, layer: str, timeout: float = 60.0):
    """GET JSON with cache-first semantics; raise :class:`DataUnavailable` naming
    ``layer`` when the network fails and nothing is cached."""
    params = params or {}
    key = cache.key(url, params)
    hit = cache.get_json(key)
    if hit is not None:
        return hit
    try:
        r = session().get(url, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise DataUnavailable(layer, f"{url}: {e}") from e
    cache.put_json(key, data)
    return data


def gdal_env() -> dict:
    """Environment for GDAL /vsicurl reads through the session proxy."""
    env = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_HTTP_MAX_RETRY": "3",
           "GDAL_HTTP_RETRY_DELAY": "1", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.vrt,.TIF,.ovr"}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        env["GDAL_HTTP_PROXY"] = proxy.replace("http://", "").replace("https://", "")
    if os.path.exists(CA_BUNDLE):
        env["CURL_CA_BUNDLE"] = CA_BUNDLE
    return env
