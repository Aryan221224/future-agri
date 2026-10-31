-- Future Agri schema.
--
-- Structure adopted from farmOS (Asset / Log / Quantity), land hierarchy from
-- OpenAgri's OCSM (farm -> site -> parcel -> management zone), observation
-- shape from OGC SensorThings (thing/datastream/observation). We implement
-- those models natively rather than adopting their runtimes: farmOS is Drupal,
-- FROST-Server is a JVM service that would duplicate our timeseries store, and
-- Eclipse Ditto -- the obvious "digital twin state store" -- has no geospatial
-- query operators at all, which disqualifies it for a system whose primary
-- query is "which twins are inside this boundary".
--
-- Two invariants the rest of the system depends on:
--   1. Nothing mutable is updated in place. Mutable objects are append-only
--      version chains, so edge/cloud sync is append-only replication and
--      conflicts are detected rather than silently merged.
--   2. Every row that can influence a field operation carries its provenance:
--      source, model_version, and for observations the calibration used.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------- land
CREATE TABLE IF NOT EXISTS farm (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS site (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    farm_id      uuid NOT NULL REFERENCES farm(id),
    name         text NOT NULL,
    geom         geometry(MultiPolygon, 4326)
);

CREATE TABLE IF NOT EXISTS parcel (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id      uuid NOT NULL REFERENCES site(id),
    name         text NOT NULL,
    -- Content-addressed identity for the ground this parcel covers. Stable
    -- under vertex reordering, ring winding and sub-metre jitter, so the same
    -- field digitised twice by two people resolves to one record.
    geoid        text NOT NULL,
    geom         geometry(Polygon, 4326) NOT NULL,
    area_ha      double precision NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_id, geoid)
);
CREATE INDEX IF NOT EXISTS parcel_geom_gix ON parcel USING GIST (geom);
CREATE INDEX IF NOT EXISTS parcel_geoid_ix ON parcel (geoid);

CREATE TABLE IF NOT EXISTS management_zone (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parcel_id    uuid NOT NULL REFERENCES parcel(id),
    name         text NOT NULL,
    geom         geometry(Polygon, 4326) NOT NULL,
    area_ha      double precision NOT NULL,
    soil         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS mz_geom_gix ON management_zone USING GIST (geom);
CREATE INDEX IF NOT EXISTS mz_parcel_ix ON management_zone (parcel_id);

-- ---------------------------------------------------------------- assets & logs
CREATE TYPE asset_type AS ENUM ('land','plant','equipment','sensor','material','structure');
CREATE TYPE log_type   AS ENUM ('observation','input','seeding','transplanting',
                                'harvest','activity','lab_test','maintenance');
CREATE TYPE log_status AS ENUM ('pending','done','abandoned');

CREATE TABLE IF NOT EXISTS asset (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type         asset_type NOT NULL,
    name         text NOT NULL,
    parent_id    uuid REFERENCES asset(id),
    geoid        text,
    geom         geometry(Geometry, 4326),
    attributes   jsonb NOT NULL DEFAULT '{}'::jsonb,
    archived     boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS log (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type           log_type NOT NULL,
    name           text NOT NULL DEFAULT '',
    ts             timestamptz NOT NULL,
    -- pending = planned (a prescription), done = executed (as-applied),
    -- abandoned = cancelled but never deleted. Plan-versus-actual is one query.
    status         log_status NOT NULL DEFAULT 'pending',
    geom           geometry(Geometry, 4326),
    notes          text NOT NULL DEFAULT '',
    source         text NOT NULL DEFAULT '',
    model_version  text,
    attributes     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS log_ts_ix ON log (ts DESC);
CREATE INDEX IF NOT EXISTS log_status_ix ON log (status) WHERE status = 'pending';

-- "happened TO" (assets) is kept distinct from "happened IN" (locations).
CREATE TABLE IF NOT EXISTS log_asset (
    log_id    uuid NOT NULL REFERENCES log(id) ON DELETE CASCADE,
    asset_id  uuid NOT NULL REFERENCES asset(id),
    PRIMARY KEY (log_id, asset_id)
);
CREATE TABLE IF NOT EXISTS log_location (
    log_id    uuid NOT NULL REFERENCES log(id) ON DELETE CASCADE,
    asset_id  uuid NOT NULL REFERENCES asset(id),
    PRIMARY KEY (log_id, asset_id)
);

CREATE TABLE IF NOT EXISTS quantity (
    id           bigserial PRIMARY KEY,
    log_id       uuid NOT NULL REFERENCES log(id) ON DELETE CASCADE,
    measure      text NOT NULL,
    value        double precision NOT NULL,
    unit         text NOT NULL,
    label        text NOT NULL DEFAULT '',
    material_id  text
);
CREATE INDEX IF NOT EXISTS quantity_log_ix ON quantity (log_id);

-- ---------------------------------------------------------------- observations
-- Derived products from flights and sensors. This is what crosses the uplink;
-- imagery stays at the edge.
CREATE TABLE IF NOT EXISTS observation (
    id              bigserial,
    observed_at     timestamptz NOT NULL,
    zone_id         uuid REFERENCES management_zone(id),
    parcel_id       uuid REFERENCES parcel(id),
    variable        text NOT NULL,
    value           double precision NOT NULL,
    unit            text NOT NULL,
    -- Calibrated bounds, not a softmax score.
    lo              double precision,
    hi              double precision,
    coverage        double precision,
    label_set       text[],
    needs_review    boolean NOT NULL DEFAULT false,
    source          text NOT NULL DEFAULT '',
    model_version   text,
    calibration     text,
    n_pixels        integer,
    -- Natural key for idempotent ingest: MQTT QoS 1 redelivers on reconnect and
    -- a store-and-forward drain WILL produce duplicates. Non-negotiable.
    dedupe_key      text NOT NULL,
    PRIMARY KEY (observed_at, dedupe_key)
);
SELECT create_hypertable('observation', by_range('observed_at', INTERVAL '7 days'),
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS obs_zone_ix ON observation (zone_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS obs_review_ix ON observation (needs_review, observed_at DESC)
    WHERE needs_review;

CREATE TABLE IF NOT EXISTS sensor_reading (
    observed_at  timestamptz NOT NULL,
    sensor_id    text NOT NULL,
    parcel_id    uuid REFERENCES parcel(id),
    metric       text NOT NULL,
    value        double precision NOT NULL,
    quality      smallint,
    -- Monotonic per-device counter, carried alongside wall-clock time so that
    -- a gateway whose RTC drifts or resets can be detected and repaired rather
    -- than silently writing a night of readings into 1970.
    seq          bigint,
    PRIMARY KEY (observed_at, sensor_id, metric)
);
SELECT create_hypertable('sensor_reading', by_range('observed_at', INTERVAL '7 days'),
                         if_not_exists => TRUE);

-- ---------------------------------------------------------------- versioned state
-- Prescriptions, boundary edits and annotations. Append-only: two versions
-- sharing a parent is a detected conflict for a human, never an auto-merge.
CREATE TABLE IF NOT EXISTS object_version (
    object_id       uuid NOT NULL,
    version_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_version  uuid,
    object_kind     text NOT NULL,
    author          text NOT NULL,
    device_id       text NOT NULL DEFAULT 'cloud',
    created_at      timestamptz NOT NULL DEFAULT now(),
    payload         jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS ov_object_ix ON object_version (object_id, created_at DESC);

CREATE OR REPLACE VIEW version_conflict AS
SELECT object_id, parent_version, count(*) AS n_children,
       array_agg(version_id) AS versions
FROM object_version
WHERE parent_version IS NOT NULL
GROUP BY object_id, parent_version
HAVING count(*) > 1;

-- ---------------------------------------------------------------- missions
CREATE TABLE IF NOT EXISTS mission (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parcel_id     uuid NOT NULL REFERENCES parcel(id),
    planned_for   date NOT NULL,
    status        log_status NOT NULL DEFAULT 'pending',
    n_sorties     integer NOT NULL,
    energy_wh     double precision NOT NULL,
    duration_s    double precision NOT NULL,
    plan          jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS coverage_alarm (
    id            bigserial PRIMARY KEY,
    raised_at     timestamptz NOT NULL DEFAULT now(),
    stratum       text NOT NULL,
    nominal       double precision NOT NULL,
    observed      double precision NOT NULL,
    n             integer NOT NULL,
    resolved_at   timestamptz
);
