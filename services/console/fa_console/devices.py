"""Field devices: soil-moisture probes, weather stations, a GPS tracker on the
drone (the K3-A Pro has no telemetry port), cameras. Devices push readings over
HTTP with their own token (shown once, stored as a SHA-256 hash)."""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from .errors import NotFound

KINDS = ("soil_moisture", "weather_station", "tracker", "camera")
READINGS = {"soil_moisture": ("%", 0, 100), "soil_temp": ("C", -30, 70), "air_temp": ("C", -50, 60),
            "rh": ("%", 0, 100), "rain_mm": ("mm", 0, 500), "wind_ms": ("m/s", 0, 75),
            "leaf_wetness": ("min", 0, 1440), "battery_v": ("V", 0, 60), "flow_lpm": ("L/min", 0, 20),
            "position": (None, None, None)}
MAX_BATCH = 5000


def token_hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


class DeviceService:
    def __init__(self, store, farms):
        self.store, self.farms = store, farms

    def create(self, fid: str, kind: str, name: str) -> dict:
        self.farms.get(fid)
        if kind not in KINDS:
            raise ValueError(f"device kind must be one of {KINDS}")
        name = name.strip()
        if not 1 <= len(name) <= 60:
            raise ValueError("device name must be 1-60 characters")
        tok = secrets.token_urlsafe(24)
        dev = self.store.add_device(fid, kind, name, token_hash(tok))
        self.store.add_event(fid, "device_added", {"device": dev["id"], "kind": kind, "name": name})
        return {"device": dev, "token": tok}

    def delete(self, fid: str, did: str) -> None:
        dev = self.store.get_device(did)
        if dev is None or dev["farm_id"] != fid:
            raise NotFound(f"no device {did}")
        self.store.delete_device(did)
        self.store.add_event(fid, "device_removed", {"device": did})

    def ingest(self, token: str, readings: list[dict]) -> dict:
        dev = self.store.device_by_token_hash(token_hash(token or ""))
        if dev is None:
            raise PermissionError("unknown device token")
        if len(readings) > MAX_BATCH:
            raise ValueError(f"at most {MAX_BATCH} readings per request")
        nowt = dt.datetime.now(dt.timezone.utc)
        ok, bad = [], []
        for i, r in enumerate(readings):
            why = self._check(r, nowt)
            if why:
                bad.append({"index": i, "reason": why})
                continue
            ts = self._ts(r["ts"]).isoformat()
            unit = READINGS[r["kind"]][0]
            ok.append({"kind": r["kind"], "ts": ts, "value": None if r["kind"] == "position" else float(r["value"]),
                       "unit": unit, "payload": r.get("payload") or {}})
        if ok:
            self.store.add_readings(dev["farm_id"], dev["id"], ok)
        self.store.touch_device(dev["id"])
        return {"accepted": len(ok), "rejected": bad, "device": dev["id"]}

    @staticmethod
    def _ts(v) -> dt.datetime:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)

    def _check(self, r: dict, nowt: dt.datetime) -> str | None:
        if not isinstance(r, dict):
            return "a reading must be an object"
        kind = r.get("kind")
        if kind not in READINGS:
            return f"unknown reading kind '{kind}'"
        try:
            t = self._ts(r.get("ts"))
        except (TypeError, ValueError):
            return "ts must be an ISO 8601 time"
        if t > nowt + dt.timedelta(days=1) or t < nowt - dt.timedelta(days=366):
            return "ts is more than a day ahead or a year behind"
        if kind == "position":
            p = r.get("payload") or {}
            try:
                lat, lon = float(p["lat"]), float(p["lon"])
            except (KeyError, TypeError, ValueError):
                return "a position needs payload.lat and payload.lon"
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return "latitude/longitude out of range"
            return None
        _, lo, hi = READINGS[kind]
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            return "value must be a number"
        if not lo <= v <= hi:
            return f"{kind} {v} outside {lo}..{hi}"
        return None

    def tracker(self, fid: str, limit: int = 600) -> dict:
        from pyproj import Transformer
        from .scene import farm_grid
        vf = self.farms.load(fid)
        grid = farm_grid(vf)
        tr = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
        rows = self.store.list_readings(fid, kind="position", limit=limit)
        out = []
        for r in rows:
            p = r["payload"]
            x, y = tr.transform(float(p["lon"]), float(p["lat"]))
            out.append({"ts": r["ts"], "device": r["device_id"], "x": round(x, 2), "y": round(y, 2),
                        "z": float(p.get("alt_m", 0.0) or 0.0), "heading_deg": p.get("heading_deg"),
                        "speed_ms": p.get("speed_ms"), "spraying": bool(p.get("spraying", False))})
        minx, miny, _, _ = grid.bounds_local
        return {"positions": out, "origin_x": round(minx, 2), "origin_y": round(miny, 2)}
