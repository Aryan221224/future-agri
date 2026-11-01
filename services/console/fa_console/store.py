"""SQLite store (WAL) for farms, jobs, the audit/event log, plans, operations,
records, devices, readings and settings. Large artefacts live as files under
``FA_HOME/farms/<id>/`` and ``FA_HOME/jobs/``; the database holds their state."""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS farms (id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL,
  template TEXT, crop TEXT, region TEXT, country TEXT, year INTEGER, lat REAL, lon REAL,
  area_ha REAL, cell_m REAL, polygon TEXT, sowing TEXT, harvest TEXT,
  status TEXT NOT NULL DEFAULT 'new', meta TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, farm_id TEXT, kind TEXT NOT NULL,
  status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
  params TEXT NOT NULL DEFAULT '{}', result TEXT, error TEXT, worker TEXT,
  target TEXT NOT NULL DEFAULT 'local', created_at TEXT NOT NULL, started_at TEXT,
  finished_at TEXT, heartbeat_at TEXT);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, farm_id TEXT,
  ts TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL DEFAULT 'operator',
  payload TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS events_farm ON events(farm_id, id);
CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, farm_id TEXT NOT NULL, status TEXT NOT NULL,
  source_job TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL, approved_at TEXT, actor TEXT);
CREATE TABLE IF NOT EXISTS operations (farm_id TEXT NOT NULL, id TEXT NOT NULL,
  payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (farm_id, id));
CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, farm_id TEXT NOT NULL, operation_id TEXT,
  ts TEXT NOT NULL, source TEXT NOT NULL, product TEXT, litres REAL, area_ha REAL,
  payload TEXT NOT NULL DEFAULT '{}', actor TEXT NOT NULL DEFAULT 'operator');
CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, farm_id TEXT NOT NULL, kind TEXT NOT NULL,
  name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, meta TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, last_seen TEXT);
CREATE TABLE IF NOT EXISTS readings (id INTEGER PRIMARY KEY AUTOINCREMENT, farm_id TEXT NOT NULL,
  device_id TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL, value REAL, unit TEXT,
  payload TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS readings_dev ON readings(farm_id, device_id, ts);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
FARM_COLS = ("name", "mode", "template", "crop", "region", "country", "year", "lat", "lon", "area_ha",
             "cell_m", "polygon", "sowing", "harvest", "status", "meta")
JOB_COLS = ("status", "progress", "message", "params", "result", "error", "worker", "target",
            "started_at", "finished_at", "heartbeat_at")
JSON_COLS = {"meta", "params", "result", "payload", "polygon"}
TERMINAL = ("done", "failed", "cancelled")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")      # farm and job ids: never a path


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.home = self.path.parent
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)

    # ---------------------------------------------------------------- helpers
    def _rows(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [self._decode(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> dict | None:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    @staticmethod
    def _decode(row) -> dict:
        d = dict(row)
        for k in JSON_COLS & d.keys():
            if d[k] is not None and isinstance(d[k], str):
                d[k] = json.loads(d[k])
        return d

    @staticmethod
    def _enc(k: str, v):
        return json.dumps(v, default=str) if k in JSON_COLS and v is not None else v

    def farm_dir(self, fid: str) -> Path:
        if not isinstance(fid, str) or not SAFE_ID.fullmatch(fid):
            raise ValueError(f"bad farm id {fid!r}")
        p = self.home / "farms" / fid
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---------------------------------------------------------------- farms
    def create_farm(self, data: dict) -> dict:
        fid = data.get("id") or new_id("f_")
        row = {k: data.get(k) for k in FARM_COLS}
        row["status"] = row["status"] or "new"
        row["meta"] = row["meta"] or {}
        t = now()
        cols = ["id", *FARM_COLS, "created_at", "updated_at"]
        vals = [fid, *[self._enc(k, row[k]) for k in FARM_COLS], t, t]
        self._exec(f"INSERT INTO farms ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return self.get_farm(fid)

    def get_farm(self, fid: str) -> dict | None:
        return self._one("SELECT * FROM farms WHERE id=?", (fid,))

    def list_farms(self) -> list[dict]:
        return self._rows("SELECT * FROM farms ORDER BY created_at, id")

    def update_farm(self, fid: str, **fields) -> dict | None:
        bad = set(fields) - set(FARM_COLS)
        if bad:
            raise ValueError(f"unknown farm fields {sorted(bad)}")
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self._exec(f"UPDATE farms SET {sets}, updated_at=? WHERE id=?",
                       [*[self._enc(k, v) for k, v in fields.items()], now(), fid])
        return self.get_farm(fid)

    def delete_farm(self, fid: str) -> None:
        with self._lock:
            for t in ("events", "plans", "operations", "records", "devices", "readings", "jobs"):
                self._db.execute(f"DELETE FROM {t} WHERE farm_id=?", (fid,))
            self._db.execute("DELETE FROM farms WHERE id=?", (fid,))
        shutil.rmtree(self.home / "farms" / fid, ignore_errors=True)

    # ---------------------------------------------------------------- events (audit + timeline)
    def add_event(self, farm_id: str | None, kind: str, payload: dict | None = None,
                  actor: str = "operator") -> int:
        cur = self._exec("INSERT INTO events (farm_id, ts, kind, actor, payload) VALUES (?,?,?,?,?)",
                         (farm_id, now(), kind, actor, json.dumps(payload or {}, default=str)))
        return int(cur.lastrowid)

    def events(self, farm_id: str | None, kinds: list[str] | None = None, limit: int = 500,
               after_id: int = 0) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE id>?", [after_id]
        if farm_id is not None:
            sql += " AND farm_id=?"
            args.append(farm_id)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        return self._rows(sql, args)

    def delete_events(self, farm_id: str, kinds: list[str]) -> None:
        self._exec(f"DELETE FROM events WHERE farm_id=? AND kind IN ({','.join('?' * len(kinds))})",
                   [farm_id, *kinds])

    # ---------------------------------------------------------------- jobs
    def create_job(self, kind: str, farm_id: str | None, params: dict, target: str = "local") -> dict:
        jid = new_id("j_")
        self._exec("INSERT INTO jobs (id, farm_id, kind, status, params, target, created_at) "
                   "VALUES (?,?,?,?,?,?,?)", (jid, farm_id, kind, "queued",
                                              json.dumps(params, default=str), target, now()))
        return self.get_job(jid)

    def get_job(self, jid: str) -> dict | None:
        return self._one("SELECT * FROM jobs WHERE id=?", (jid,))

    def list_jobs(self, farm_id: str | None = None, limit: int = 100, kind: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if farm_id is not None:
            sql += " AND farm_id=?"
            args.append(farm_id)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        return self._rows(sql, args)

    def update_job(self, jid: str, **fields) -> dict | None:
        bad = set(fields) - set(JOB_COLS)
        if bad:
            raise ValueError(f"unknown job fields {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {sets} WHERE id=?", [*[self._enc(k, v) for k, v in fields.items()], jid])
        return self.get_job(jid)

    def claim_job(self, kinds: list[str], worker: str, targets: tuple[str, ...]) -> dict | None:
        if not kinds:
            return None
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    f"SELECT id FROM jobs WHERE status='queued' AND kind IN ({','.join('?' * len(kinds))}) "
                    f"AND target IN ({','.join('?' * len(targets))}) ORDER BY created_at, rowid LIMIT 1",
                    [*kinds, *targets]).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return None
                t = now()
                self._db.execute("UPDATE jobs SET status='running', worker=?, started_at=?, heartbeat_at=? "
                                 "WHERE id=?", (worker, t, t, row["id"]))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return self.get_job(row["id"])

    # ---------------------------------------------------------------- plans
    def add_plan(self, farm_id: str, payload: dict, source_job: str | None = None,
                 status: str = "draft") -> dict:
        pid = new_id("p_")
        self._exec("INSERT INTO plans (id, farm_id, status, source_job, payload, created_at) VALUES (?,?,?,?,?,?)",
                   (pid, farm_id, status, source_job, json.dumps(payload, default=str), now()))
        return self.get_plan(pid)

    def get_plan(self, pid: str) -> dict | None:
        return self._one("SELECT * FROM plans WHERE id=?", (pid,))

    def list_plans(self, farm_id: str) -> list[dict]:
        return self._rows("SELECT * FROM plans WHERE farm_id=? ORDER BY created_at DESC, rowid DESC", (farm_id,))

    def set_plan_status(self, pid: str, status: str, actor: str = "operator") -> dict | None:
        if status == "approved":
            self._exec("UPDATE plans SET status='approved', approved_at=?, actor=? WHERE id=?", (now(), actor, pid))
        else:
            self._exec("UPDATE plans SET status=? WHERE id=?", (status, pid))
        return self.get_plan(pid)

    def approved_plan(self, farm_id: str) -> dict | None:
        return self._one("SELECT * FROM plans WHERE farm_id=? AND status='approved' "
                         "ORDER BY approved_at DESC LIMIT 1", (farm_id,))

    # ---------------------------------------------------------------- operations (mirror of the session)
    def put_operations(self, farm_id: str, ops: list[dict]) -> None:
        t = now()
        with self._lock:
            self._db.execute("DELETE FROM operations WHERE farm_id=?", (farm_id,))
            self._db.executemany("INSERT INTO operations (farm_id, id, payload, updated_at) VALUES (?,?,?,?)",
                                 [(farm_id, o["id"], json.dumps(o, default=str), t) for o in ops])

    def list_operations(self, farm_id: str) -> list[dict]:
        return [r["payload"] for r in self._rows("SELECT payload FROM operations WHERE farm_id=? ORDER BY rowid",
                                                 (farm_id,))]

    # ---------------------------------------------------------------- records
    def add_record(self, farm_id: str, rec: dict, source: str, actor: str = "operator") -> dict:
        rid = rec.get("id") or new_id("r_")
        self._exec("INSERT INTO records (id, farm_id, operation_id, ts, source, product, litres, area_ha, payload, actor)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (rid, farm_id, rec.get("operation_id"), rec.get("ts") or now(), source, rec.get("product"),
                    rec.get("litres"), rec.get("area_ha"), json.dumps(rec, default=str), actor))
        return self._one("SELECT * FROM records WHERE id=?", (rid,))

    def list_records(self, farm_id: str) -> list[dict]:
        return self._rows("SELECT * FROM records WHERE farm_id=? ORDER BY ts, rowid", (farm_id,))

    # ---------------------------------------------------------------- devices and readings
    def add_device(self, farm_id: str, kind: str, name: str, token_hash: str, meta: dict | None = None) -> dict:
        did = new_id("d_")
        self._exec("INSERT INTO devices (id, farm_id, kind, name, token_hash, meta, created_at) VALUES (?,?,?,?,?,?,?)",
                   (did, farm_id, kind, name, token_hash, json.dumps(meta or {}), now()))
        return self.get_device(did)

    def get_device(self, did: str) -> dict | None:
        d = self._one("SELECT * FROM devices WHERE id=?", (did,))
        if d:
            d.pop("token_hash", None)
        return d

    def device_by_token_hash(self, token_hash: str) -> dict | None:
        d = self._one("SELECT * FROM devices WHERE token_hash=?", (token_hash,))
        if d:
            d.pop("token_hash", None)
        return d

    def list_devices(self, farm_id: str) -> list[dict]:
        out = self._rows("SELECT * FROM devices WHERE farm_id=? ORDER BY created_at", (farm_id,))
        for d in out:
            d.pop("token_hash", None)
        return out

    def touch_device(self, did: str) -> None:
        self._exec("UPDATE devices SET last_seen=? WHERE id=?", (now(), did))

    def delete_device(self, did: str) -> None:
        self._exec("DELETE FROM devices WHERE id=?", (did,))

    def add_readings(self, farm_id: str, device_id: str, rows: list[dict]) -> int:
        with self._lock:
            self._db.executemany(
                "INSERT INTO readings (farm_id, device_id, kind, ts, value, unit, payload) VALUES (?,?,?,?,?,?,?)",
                [(farm_id, device_id, r["kind"], r["ts"], r.get("value"), r.get("unit"),
                  json.dumps(r.get("payload") or {})) for r in rows])
        return len(rows)

    def list_readings(self, farm_id: str, kind: str | None = None, since: str | None = None,
                      device_id: str | None = None, limit: int = 2000) -> list[dict]:
        sql, args = "SELECT * FROM readings WHERE farm_id=?", [farm_id]
        for col, v in (("kind", kind), ("device_id", device_id)):
            if v is not None:
                sql += f" AND {col}=?"
                args.append(v)
        if since is not None:
            sql += " AND ts>=?"
            args.append(since)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return list(reversed(self._rows(sql, args)))

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str, default=None):
        r = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value) -> None:
        self._exec("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    def settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self._rows("SELECT key, value FROM settings")}
