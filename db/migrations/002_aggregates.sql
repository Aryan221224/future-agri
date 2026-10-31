-- Continuous aggregates for the dashboard.
--
-- The start_offset below is 30 days and that is deliberate, not generous.
-- A field gateway that is offline for a week drains its queue on reconnect and
-- writes backdated rows. Any row older than start_offset lands BEHIND the
-- refresh window and never enters the aggregate -- the dashboard then shows a
-- clean, wrong, silently-incomplete chart, and nothing warns you. Size this to
-- the worst realistic disconnection, or call refresh_continuous_aggregate()
-- explicitly after a bulk backfill.

CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', observed_at) AS bucket,
       sensor_id, metric, parcel_id,
       avg(value) AS avg_value, min(value) AS min_value,
       max(value) AS max_value, count(*) AS n
FROM sensor_reading
GROUP BY bucket, sensor_id, metric, parcel_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('sensor_hourly',
    start_offset => INTERVAL '30 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS observation_daily
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', observed_at) AS bucket,
       zone_id, variable,
       avg(value) AS avg_value,
       avg(hi - lo) AS avg_interval_width,
       sum(CASE WHEN needs_review THEN 1 ELSE 0 END) AS review_count,
       count(*) AS n
FROM observation
GROUP BY bucket, zone_id, variable
WITH NO DATA;

SELECT add_continuous_aggregate_policy('observation_daily',
    start_offset => INTERVAL '30 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);
