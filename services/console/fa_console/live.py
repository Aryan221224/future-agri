"""Live feeds for a farm: weather observed and forecast, the spray outlook, the
satellite record, and alerts.

Live fields read the latest published data (NASA POWER near real time, the newest
ECMWF/GEFS ensemble issue, Sentinel-2 scenes). Virtual farms read the same feeds
as the twin saw them on the session's current day (recorded season, archived
forecast issued that day). Every feed is cached per farm; when a fetch fails the
last good payload is served with ``stale: true``, and with no cache at all the
feed reports ``available: false`` and why -- never an error page.
"""
from __future__ import annotations

import datetime as dt
import json
import time

import numpy as np

from fa_drone.spec import load_spec
from fa_farm.regions import load_region
from fa_farm.sources.base import DataUnavailable
from fa_farm.sources.forecasts import ForecastSource, ensemble_forecast, latest_forecast
from fa_runner.scenarios import flight_record, spray_window

from .store import now

PCT = (10, 50, 90)


def _pcts(vals) -> list[float]:
    return [round(float(v), 2) for v in np.percentile(np.asarray(vals, float), PCT)]


def _alert(level: str, code: str, text: str, **params) -> dict:
    return {"level": level, "code": code, "text": text, "params": params}


class LiveService:
    def __init__(self, store, settings, farms, sessions):
        self.store, self.settings, self.farms, self.sessions = store, settings, farms, sessions
        self.ttl = {"weather": 3 * 3600, "outlook": 3 * 3600, "satellite": 12 * 3600}

    # ---------------------------------------------------------------- caching
    def _cached(self, fid: str, kind: str, fetch) -> dict:
        d = self.store.farm_dir(fid) / "live"
        d.mkdir(exist_ok=True)
        p = d / f"{kind}.json"
        hit = json.loads(p.read_text()) if p.exists() else None
        if hit is not None and time.time() - hit["ts"] < self.ttl[kind]:
            return {**hit["data"], "available": True, "stale": False, "fetched": hit["iso"]}
        try:
            data = fetch()
        except Exception as e:  # noqa: BLE001 -- any failed fetch degrades to cache
            if hit is not None:
                return {**hit["data"], "available": True, "stale": True, "stale_reason": str(e),
                        "fetched": hit["iso"]}
            return {"available": False, "stale": False, "reason": str(e)}
        iso = now()
        p.write_text(json.dumps({"ts": time.time(), "iso": iso, "data": data}, default=str))
        return {**data, "available": True, "stale": False, "fetched": iso}

    def _day(self, fid: str, farm: dict) -> dt.date:
        if farm["mode"] == "field":
            return dt.date.today()
        return dt.date.fromisoformat(self.sessions.state(fid)["day"])

    # ---------------------------------------------------------------- fetchers (overridable in tests)
    def _forecast_members(self, farm: dict, vf):
        lat, lon = vf.spec.centroid
        cache = self.settings.data_cache()
        if farm["mode"] == "sim":
            day = self._day(farm["id"], farm)
            sources = [ForecastSource.CLIMATOLOGY] if self.settings.offline else None
            mem, src, prov = ensemble_forecast(lat, lon, day, cache, vf.history, n_members=20, sources=sources)
            return mem, src.value, day.isoformat()
        if self.settings.offline:
            raise DataUnavailable("forecast", "offline")
        mem, src, issue, prov = latest_forecast(lat, lon, cache, n_members=20)
        return mem, src.value, issue.isoformat()

    def _fetch_weather(self, farm: dict, vf) -> dict:
        if farm["mode"] == "sim":
            day = self._day(farm["id"], farm)
            recs = [r for r in vf.season_weather if r["DAY"] < day][-30:]
            note = f"recorded season as seen on {day} (virtual farm)"
        else:
            from fa_farm.sources.weather import power_recent
            lat, lon = vf.spec.centroid
            recs, prov, _ = power_recent(lat, lon, dt.date.today() - dt.timedelta(days=35),
                                         self.settings.data_cache())
            recs = recs[-30:]
            note = prov.note
        observed = [{"day": r["DAY"].isoformat(), "tmax": round(r["TMAX"], 1), "tmin": round(r["TMIN"], 1),
                     "rain_mm": round(r["RAIN"], 1), "wind_ms": round(r["WIND"], 2),
                     "rh": round(r.get("RH", float("nan")), 0) if r.get("RH") is not None else None}
                    for r in recs]
        try:
            mem, src, issue = self._forecast_members(farm, vf)
            days = []
            for k in range(min(len(m) for m in mem)):
                day_recs = [m[k] for m in mem]
                days.append({"day": day_recs[0]["DAY"].isoformat(),
                             "tmax": _pcts([r["TMAX"] for r in day_recs]), "tmin": _pcts([r["TMIN"] for r in day_recs]),
                             "rain_mm": _pcts([r["RAIN"] for r in day_recs]),
                             "wind_ms": _pcts([r["WIND"] for r in day_recs])})
            forecast = {"source": src, "issue": issue, "members": len(mem), "days": days,
                        "percentiles": list(PCT)}
        except DataUnavailable as e:
            forecast = {"source": None, "reason": str(e), "days": []}
        return {"observed": observed, "forecast": forecast, "note": note}

    # ---------------------------------------------------------------- feeds
    def weather(self, fid: str) -> dict:
        farm = self.farms.get(fid)
        vf = self.farms.load(fid)
        return self._cached(fid, "weather", lambda: self._fetch_weather(farm, vf))

    def outlook(self, fid: str) -> dict:
        farm = self.farms.get(fid)
        vf = self.farms.load(fid)
        region = load_region(farm["region"] or vf.spec.archetype.region)
        window = spray_window(region, load_spec())

        def fetch():
            mem, src, issue = self._forecast_members(farm, vf)
            days = []
            for k in range(min(len(m) for m in mem)):
                recs = [m[k] for m in mem]
                ok = [window.ok(flight_record(r)) for r in recs]
                days.append({"day": recs[0]["DAY"].isoformat(), "p_flyable": round(float(np.mean(ok)), 3),
                             "wind_dawn_p50": round(float(np.median([flight_record(r)["WIND"] for r in recs])), 2),
                             "rain_p50": round(float(np.median([r["RAIN"] for r in recs])), 2),
                             "tmax_p50": round(float(np.median([r["TMAX"] for r in recs])), 1)})
            return {"source": src, "issue": issue, "days": days,
                    "limits": {"wind_ms": window.max_wind_ms, "rain_mm": window.max_rain_mm,
                               "tmax_c": window.max_tmax_c},
                    "note": "dawn wind = daily-mean 2 m wind x 0.7 (drones fly at dawn/dusk); limits are the "
                            "stricter of the region pack and the aircraft"}
        return self._cached(fid, "outlook", fetch)

    def satellite(self, fid: str) -> dict:
        farm = self.farms.get(fid)
        vf = self.farms.load(fid)

        def fetch():
            series = list(getattr(vf, "s2_series", []) or [])
            if farm["mode"] == "sim":
                day = self._day(fid, farm)
                series = [s for s in series if s[0] < day]
            elif not self.settings.offline:
                from fa_farm.sources.satellite import s2_cover_series
                sow = dt.date.fromisoformat(farm["sowing"])
                if sow <= dt.date.today():
                    series, _ = s2_cover_series(vf.spec.polygon_wgs84, sow - dt.timedelta(days=20),
                                                dt.date.today(), self.settings.data_cache())
            latest = {"available": False}
            scene = self.store.farm_dir(fid) / "scene.json"
            if scene.exists():
                img = json.loads(scene.read_text()).get("imagery") or {}
                if img.get("available"):
                    latest = {"available": True, "url": img["url"], "date": img.get("date"),
                              "cloud": img.get("cloud")}
            return {"series": [{"date": d.isoformat(), "ndvi": round(float(nd), 3), "cover": round(float(c), 3)}
                               for d, nd, c in series], "latest": latest,
                    "source": "Sentinel-2 L2A (Copernicus) via Earth Search"}
        return self._cached(fid, "satellite", fetch)

    def alerts(self, fid: str) -> dict:
        farm = self.farms.get(fid)
        out: list[dict] = []
        region = load_region(farm["region"])
        if not region.rules.verified:
            out.append(_alert("info", "region_unverified", f"The {region.id} regulation pack is not verified; "
                              "check local law before flying", region=region.id))
        if not region.rules.aerial_default_allowed:
            out.append(_alert("warn", "region_derogation", f"Aerial spraying needs a derogation in {region.id}",
                              region=region.id))
        try:
            st = self.sessions.state(fid)
        except Exception as e:  # noqa: BLE001
            return {"alerts": out + [_alert("info", "session_unavailable", str(e))]}
        for o in st["ops"]:
            if o["status"] != "proposed":
                continue
            if o["product"] == "scan":
                out.append(_alert("warn", "scan_due", f"Camera-drone scan due {o['due']}: fly it and upload the "
                                  "detections", due=o["due"]))
            else:
                out.append(_alert("warn", "proposal_waiting", f"{o['product'].capitalize()} proposed for {o['due']}: "
                                  "approve, defer or reject", product=o["product"], due=o["due"], op=o["id"]))
        approved = [o for o in st["ops"] if o["status"] == "approved"]
        if approved:
            ol = self.outlook(fid)
            good = [d for d in ol.get("days", []) if d["p_flyable"] >= 0.5]
            o = approved[0]
            if good:
                out.append(_alert("info", "spray_window", f"Next likely spray window for {o['product']}: "
                                  f"{good[0]['day']} ({100 * good[0]['p_flyable']:.0f}% of forecast members flyable)",
                                  product=o["product"], day=good[0]["day"], p=good[0]["p_flyable"]))
            elif ol.get("available"):
                out.append(_alert("warn", "no_window", f"No likely spray window for {o['product']} in the forecast",
                                  product=o["product"]))
        plans = [e for e in st.get("recent_events", []) if e["kind"] == "plan"]
        if plans and (plans[-1].get("p_spray_better") or 0) >= 0.5:
            out.append(_alert("warn", "blight_risk", "The twin expects a fungicide to pay off this week",
                              p=plans[-1]["p_spray_better"]))
        if st["world"].get("waiting"):
            out.append(_alert("info", "weather_lag", st["world"]["waiting"]))
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
        for dev in self.store.list_devices(fid):
            if not dev["last_seen"] or dev["last_seen"] < cutoff:
                out.append(_alert("warn", "device_silent", f"{dev['name']} has not reported for 24 hours",
                                  device=dev["id"], name=dev["name"]))
        return {"alerts": out, "day": st["day"]}
