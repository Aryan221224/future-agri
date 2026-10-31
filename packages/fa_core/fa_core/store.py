"""Embedded store: the same schema, with no server and no root.

Why this exists alongside the PostGIS/TimescaleDB stack
------------------------------------------------------
The Docker compose stack is right for a cloud or on-prem deployment. It is
unusable on a university HPC cluster, and that is where this project will
actually run first. Shared clusters give you no root, no Docker (it is
root-equivalent, so admins refuse it), no persistent daemons, and -- the one
that surprises people -- **no outbound internet from compute nodes**, which
silently breaks anything that fetches at runtime.

DuckDB is the right answer: a single file, `pip install duckdb`, no server, and
genuinely good analytical SQL. But note what happened when this module was
first written: `INSTALL spatial` failed with HTTP 403, because DuckDB downloads
its extensions on first use. On a compute node that has no internet, it fails
the same way -- so a store that *requires* the spatial extension would work on a
laptop and die on the cluster.

So geometry is stored as WKB and handled with Shapely, which is already a hard
dependency and is pure Python plus a wheel. The spatial extension is used when
it happens to be available and is never required. The store degrades to "works
everywhere" rather than "works where the internet does".
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS parcel (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    geoid       TEXT NOT NULL,
    geom_wkb    BLOB,
    area_ha     DOUBLE,
    crop        TEXT,
    variety     TEXT,
    created_at  TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS subplot (
    id          TEXT PRIMARY KEY,
    parcel_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    treatment   TEXT,
    replicate   INTEGER,
    geom_wkb    BLOB,
    area_m2     DOUBLE
);

CREATE TABLE IF NOT EXISTS station (
    id          TEXT PRIMARY KEY,
    parcel_id   TEXT NOT NULL,
    subplot_id  TEXT,
    lat         DOUBLE,
    lon         DOUBLE,
    stratum     TEXT
);

-- Derived products. This is what the twin consumes.
CREATE TABLE IF NOT EXISTS observation (
    observed_at   TIMESTAMP NOT NULL,
    subplot_id    TEXT,
    station_id    TEXT,
    variable      TEXT NOT NULL,
    value         DOUBLE NOT NULL,
    unit          TEXT,
    lo            DOUBLE,
    hi            DOUBLE,
    coverage      DOUBLE,
    needs_review  BOOLEAN DEFAULT FALSE,
    source        TEXT,
    model_version TEXT,
    calibration   TEXT,
    -- Natural key. Ingest is idempotent because a field visit gets re-ingested
    -- more often than anyone expects: a reshoot, a corrected label, a rerun.
    dedupe_key    TEXT NOT NULL,
    PRIMARY KEY (observed_at, dedupe_key)
);

-- Growth staging. Small table, carries most of the calibration signal.
CREATE TABLE IF NOT EXISTS phenology (
    observed_at  TIMESTAMP NOT NULL,
    subplot_id   TEXT NOT NULL,
    scale        TEXT NOT NULL DEFAULT 'BBCH',
    stage        DOUBLE NOT NULL,
    dvs          DOUBLE,
    observer     TEXT,
    note         TEXT,
    PRIMARY KEY (observed_at, subplot_id)
);

-- Everything done to the crop. The twin cannot infer these and will
-- misattribute their effects if they are missing.
CREATE TABLE IF NOT EXISTS operation (
    id           TEXT PRIMARY KEY,
    subplot_id   TEXT NOT NULL,
    performed_at TIMESTAMP NOT NULL,
    kind         TEXT NOT NULL,
    product      TEXT,
    rate         DOUBLE,
    unit         TEXT,
    planned      BOOLEAN DEFAULT FALSE,
    source       TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS detection (
    id            TEXT PRIMARY KEY,
    observed_at   TIMESTAMP NOT NULL,
    subplot_id    TEXT,
    image_sha1    TEXT,
    label         TEXT NOT NULL,
    label_set     TEXT,
    score         DOUBLE,
    bbox          TEXT,
    needs_review  BOOLEAN DEFAULT FALSE,
    reviewed_as   TEXT,
    model_version TEXT
);

CREATE TABLE IF NOT EXISTS capture (
    sha1        TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    subplot_id  TEXT,
    kind        TEXT,
    taken_at    TIMESTAMP,
    iso         DOUBLE,
    exposure_s  DOUBLE,
    white_bal   TEXT,
    lat         DOUBLE,
    lon         DOUBLE,
    width       INTEGER,
    height      INTEGER
);

CREATE TABLE IF NOT EXISTS run (
    id           TEXT PRIMARY KEY,
    started_at   TIMESTAMP DEFAULT current_timestamp,
    kind         TEXT,
    config       TEXT,
    result       TEXT,
    code_version TEXT
);
"""


def _wkb(geom) -> bytes | None:
    if geom is None:
        return None
    from shapely.geometry import shape
    from shapely import wkb as shp_wkb
    if isinstance(geom, dict):
        geom = shape(geom)
    return shp_wkb.dumps(geom)


def _from_wkb(blob):
    if blob is None:
        return None
    from shapely import wkb as shp_wkb
    return shp_wkb.loads(bytes(blob))


class Store:
    """A single-file store with the same shape as the server schema.

    Deliberately not an ORM. The queries this system asks are analytical and
    few; an abstraction layer would cost more than it saves.
    """

    def __init__(self, path: str | Path = "futureagri.duckdb", read_only: bool = False):
        import duckdb
        self.path = str(path)
        self.con = duckdb.connect(self.path, read_only=read_only)
        self.spatial = False
        if not read_only:
            try:
                self.con.execute("INSTALL spatial; LOAD spatial;")
                self.spatial = True
            except Exception as e:
                # Expected on an offline compute node. Geometry work falls back
                # to Shapely, which needs no download.
                log.info("duckdb spatial extension unavailable (%s); "
                         "using shapely for geometry", type(e).__name__)
            self.con.execute(SCHEMA)

    # -- writes -----------------------------------------------------------
    def add_parcel(self, id: str, name: str, geoid: str, geom=None,
                   area_ha: float | None = None, crop: str = "", variety: str = ""):
        self.con.execute(
            "INSERT OR REPLACE INTO parcel (id,name,geoid,geom_wkb,area_ha,crop,variety) "
            "VALUES (?,?,?,?,?,?,?)",
            [id, name, geoid, _wkb(geom), area_ha, crop, variety])

    def add_subplot(self, id: str, parcel_id: str, name: str, treatment: str = "",
                    replicate: int | None = None, geom=None, area_m2: float | None = None):
        self.con.execute(
            "INSERT OR REPLACE INTO subplot VALUES (?,?,?,?,?,?,?)",
            [id, parcel_id, name, treatment, replicate, _wkb(geom), area_m2])

    def add_station(self, id: str, parcel_id: str, lat: float, lon: float,
                    subplot_id: str | None = None, stratum: str = ""):
        self.con.execute(
            "INSERT OR REPLACE INTO station (id,parcel_id,subplot_id,lat,lon,stratum) "
            "VALUES (?,?,?,?,?,?)",
                         [id, parcel_id, subplot_id, lat, lon, stratum])

    def add_observation(self, observed_at, variable: str, value: float,
                        dedupe_key: str, unit: str = "", subplot_id: str | None = None,
                        station_id: str | None = None, lo=None, hi=None,
                        coverage=None, needs_review: bool = False, source: str = "",
                        model_version: str | None = None, calibration: str | None = None):
        # Columns named explicitly rather than positionally: a schema change
        # then fails loudly at the right place instead of silently shifting
        # values into neighbouring columns.
        self.con.execute(
            "INSERT OR IGNORE INTO observation (observed_at,subplot_id,station_id,"
            "variable,value,unit,lo,hi,coverage,needs_review,source,model_version,"
            "calibration,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [observed_at, subplot_id, station_id, variable, value, unit, lo, hi,
             coverage, needs_review, source, model_version, calibration, dedupe_key])

    def add_phenology(self, observed_at, subplot_id: str, stage: float,
                      scale: str = "BBCH", dvs: float | None = None,
                      observer: str = "", note: str = ""):
        self.con.execute(
            "INSERT OR REPLACE INTO phenology (observed_at,subplot_id,scale,stage,"
            "dvs,observer,note) VALUES (?,?,?,?,?,?,?)",
            [observed_at, subplot_id, scale, stage, dvs, observer, note])

    def add_operation(self, id: str, subplot_id: str, performed_at, kind: str,
                      product: str = "", rate: float | None = None, unit: str = "",
                      planned: bool = False, source: str = "", note: str = ""):
        self.con.execute(
            "INSERT OR REPLACE INTO operation (id,subplot_id,performed_at,kind,"
            "product,rate,unit,planned,source,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [id, subplot_id, performed_at, kind, product, rate, unit,
             planned, source, note])

    def add_detection(self, id: str, observed_at, label: str, subplot_id=None,
                      image_sha1: str = "", label_set: Iterable[str] = (),
                      score: float | None = None, bbox=None,
                      needs_review: bool = False, model_version: str = ""):
        self.con.execute(
            "INSERT OR REPLACE INTO detection (id,observed_at,subplot_id,image_sha1,"
            "label,label_set,score,bbox,needs_review,reviewed_as,model_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         [id, observed_at, subplot_id, image_sha1, label,
                          json.dumps(list(label_set)), score,
                          json.dumps(list(bbox)) if bbox is not None else None,
                          needs_review, None, model_version])

    def add_capture(self, cap) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO capture (sha1,path,subplot_id,kind,taken_at,iso,"
            "exposure_s,white_bal,lat,lon,width,height) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         [cap.sha1, str(cap.path), cap.subplot, cap.kind, cap.taken_at,
                          cap.iso, cap.exposure_s, cap.white_balance, cap.lat, cap.lon,
                          cap.width, cap.height])

    def log_run(self, id: str, kind: str, config: dict, result: dict,
                code_version: str = ""):
        self.con.execute(
            "INSERT OR REPLACE INTO run (id,kind,config,result,code_version) VALUES (?,?,?,?,?)",
            [id, kind, json.dumps(config, default=str),
             json.dumps(result, default=str), code_version])

    # -- reads ------------------------------------------------------------
    def df(self, sql: str, params: list | None = None):
        return self.con.execute(sql, params or []).df()

    def review_queue(self, limit: int = 100):
        return self.df(
            "SELECT observed_at, subplot_id, label, label_set, score, model_version "
            "FROM detection WHERE needs_review AND reviewed_as IS NULL "
            "ORDER BY observed_at DESC LIMIT ?", [limit])

    def observation_series(self, variable: str, subplot_id: str | None = None):
        return self.df(
            "SELECT observed_at, subplot_id, value, lo, hi FROM observation "
            "WHERE variable = ? AND (? IS NULL OR subplot_id = ?) ORDER BY observed_at",
            [variable, subplot_id, subplot_id])

    def treatment_summary(self):
        """Plot/actual comparison by treatment -- the experiment's headline table."""
        return self.df("""
            SELECT s.treatment,
                   count(DISTINCT s.id)                     AS subplots,
                   avg(o.value) FILTER (WHERE o.variable='canopy_cover') AS mean_cover,
                   avg(o.value) FILTER (WHERE o.variable='lai')          AS mean_lai,
                   sum(op.rate) FILTER (WHERE op.kind='fertilise')       AS total_n
            FROM subplot s
            LEFT JOIN observation o ON o.subplot_id = s.id
            LEFT JOIN operation  op ON op.subplot_id = s.id
            GROUP BY s.treatment ORDER BY s.treatment""")

    def parcel_geom(self, parcel_id: str):
        row = self.con.execute("SELECT geom_wkb FROM parcel WHERE id = ?",
                               [parcel_id]).fetchone()
        return _from_wkb(row[0]) if row else None

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
