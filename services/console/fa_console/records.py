"""The spray log: records imported from the aircraft (JIYI job records), entered by
hand, or recorded through the mission; each is checked against the farm's
regulation pack's record-keeping fields."""
from __future__ import annotations

import datetime as dt

from fa_drone.jiyi_io import import_job_records
from fa_farm.regions import load_region

from .errors import TooLarge

PRODUCTS = ("herbicide", "fungicide", "insecticide", "fertiliser", "other")
# regulation record field -> how a stored record answers it
FIELD_MAP = {
    "date": lambda r: r.get("ts") or r.get("day"),
    "start_time": lambda r: (r.get("ts") or "")[11:16] or r.get("start_time"),
    "product": lambda r: r.get("product") if r.get("product") not in (None, "", "unknown") else None,
    "rate_l_ha": lambda r: r.get("dose_l_ha") or (r["litres"] / r["area_ha"] if r.get("litres") and r.get("area_ha") else None),
    "area_ha": lambda r: r.get("area_ha"),
    "aircraft_id": lambda r: r.get("aircraft_id") or r.get("fc_serial"),
    "operator": lambda r: r.get("operator"),
    "wind_speed_ms": lambda r: r.get("wind_ms"),
    "wind_direction_deg": lambda r: r.get("wind_dir_deg"),
    "temperature_c": lambda r: r.get("temp_c"),
    "humidity_pct": lambda r: r.get("rh_pct"),
    "buffer_m": lambda r: r.get("buffer_m"),
}


class RecordService:
    def __init__(self, store, settings, farms):
        self.store, self.settings, self.farms = store, settings, farms

    def compliance(self, farm: dict, rec: dict) -> list[str]:
        fields = list(load_region(farm["region"]).rules.records)
        missing = []
        for f in fields:
            get = FIELD_MAP.get(f, lambda r, f=f: r.get(f))
            v = get(rec)
            if v is None or v == "":
                missing.append(f)
        return missing

    def list(self, fid: str) -> list[dict]:
        farm = self.farms.get(fid)
        out = []
        for r in self.store.list_records(fid):
            body = {**r["payload"], **{k: r[k] for k in ("ts", "product", "litres", "area_ha") if r.get(k) is not None}}
            out.append({**r, "missing": self.compliance(farm, body)})
        return out

    def add_manual(self, fid: str, rec: dict) -> dict:
        farm = self.farms.get(fid)
        if rec.get("product") not in PRODUCTS:
            raise ValueError(f"product must be one of {PRODUCTS}")
        try:
            ts = dt.datetime.fromisoformat(str(rec["ts"]).replace("Z", "+00:00"))
            litres, area = float(rec["litres"]), float(rec["area_ha"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError("a record needs ts (ISO time), litres and area_ha") from e
        if litres < 0 or area <= 0:
            raise ValueError("litres must be >= 0 and area_ha > 0")
        clean = {**rec, "ts": ts.isoformat(), "litres": litres, "area_ha": area,
                 "dose_l_ha": round(litres / area, 3)}
        stored = self.store.add_record(fid, clean, source="manual")
        self.store.add_event(fid, "record_added", {"record": stored["id"], "source": "manual"})
        return {**stored, "missing": self.compliance(farm, clean)}

    def import_jiyi(self, fid: str, data: bytes, filename: str, mapping: dict | None = None) -> dict:
        self.farms.get(fid)
        if len(data) > self.settings.max_upload_mb * 1024 * 1024:
            raise TooLarge(f"upload larger than {self.settings.max_upload_mb} MB")
        for enc in ("utf-8-sig", "gb18030", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        try:
            recs, errors = import_job_records(text, mapping)
        except (ValueError, TypeError) as e:
            raise ValueError(f"{filename}: not a JIYI job-record CSV/JSON ({e})") from e
        rows = len(recs) + len(errors)
        for jr in recs:
            self.store.add_record(fid, {
                "ts": jr.start.isoformat() if jr.start else None, "product": "unknown",
                "litres": jr.total_l, "area_ha": round(jr.area_ha, 4), "dose_l_ha": jr.dose_l_ha,
                "flight_min": jr.flight_min, "fc_serial": jr.fc_serial, "distance_m": jr.distance_m,
                "crop": jr.crop, "file": filename}, source="jiyi")
        self.store.add_event(fid, "records_imported", {"file": filename, "added": len(recs), "errors": len(errors)})
        return {"added": len(recs), "report": {"rows": rows, "errors": errors, "file": filename}}
