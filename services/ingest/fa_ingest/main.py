"""MQTT -> TimescaleDB ingest.

Every design decision here is about surviving the store-and-forward drain, which
is the normal case in rural deployment rather than an edge case.

* **Idempotent writes.** QoS 1 is at-least-once and the reconnect drain *will*
  redeliver. Every insert is ``ON CONFLICT DO NOTHING`` against a natural key.
  Without this, a week offline produces a week of duplicates.
* **Never derive state from arrival order.** Backfilled messages arrive
  interleaved with live ones. Time comes from the sensor's own stamp.
* **Clock skew is detected, not assumed away.** Readings carry a monotonic
  per-device sequence number alongside wall-clock time. A gateway whose RTC
  reset stamps a whole outage into 1970; the sequence number is what lets that
  be found and repaired afterwards.
* **Bounded queues, on both sides.** The broker config caps by *bytes*, not
  message count -- Mosquitto's ``max_queued_messages`` default of 1000 is about
  twelve minutes of buffer for a modest sensor set, and it discards the excess
  silently.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone

log = logging.getLogger("fa.ingest")

DSN = os.environ.get("FA_DSN", "postgresql://futureagri:futureagri@db:5432/futureagri")
BROKER = os.environ.get("FA_MQTT_HOST", "mqtt")
PORT = int(os.environ.get("FA_MQTT_PORT", "1883"))
TOPICS = os.environ.get("FA_MQTT_TOPICS", "farm/+/sensor/#,farm/+/observation/#")

INSERT_SENSOR = """
INSERT INTO sensor_reading (observed_at, sensor_id, parcel_id, metric, value, quality, seq)
VALUES ($1,$2,$3,$4,$5,$6,$7)
ON CONFLICT (observed_at, sensor_id, metric) DO NOTHING
"""

INSERT_OBS = """
INSERT INTO observation (observed_at, zone_id, parcel_id, variable, value, unit,
                         lo, hi, coverage, label_set, needs_review, source,
                         model_version, calibration, n_pixels, dedupe_key)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
ON CONFLICT (observed_at, dedupe_key) DO NOTHING
"""


def _ts(value) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class Ingest:
    def __init__(self):
        self.pool = None
        self.n_sensor = 0
        self.n_obs = 0
        self.n_dupe_batches = 0

    async def start(self):
        import asyncpg
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    async def handle(self, topic: str, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
        except Exception:
            log.warning("undecodable payload on %s (%d bytes)", topic, len(payload))
            return
        async with self.pool.acquire() as con:
            if "/sensor/" in topic:
                await con.execute(
                    INSERT_SENSOR, _ts(msg.get("ts")), str(msg["sensor_id"]),
                    msg.get("parcel_id"), str(msg["metric"]), float(msg["value"]),
                    msg.get("quality"), msg.get("seq"))
                self.n_sensor += 1
            elif "/observation/" in topic:
                # Dedupe key must be stable across redelivery and unique per
                # logical observation.
                key = msg.get("dedupe_key") or "|".join(
                    str(msg.get(k, "")) for k in ("zone_id", "variable", "source", "ts"))
                await con.execute(
                    INSERT_OBS, _ts(msg.get("ts")), msg.get("zone_id"),
                    msg.get("parcel_id"), str(msg["variable"]), float(msg["value"]),
                    msg.get("unit", ""), msg.get("lo"), msg.get("hi"),
                    msg.get("coverage"), msg.get("label_set"),
                    bool(msg.get("needs_review", False)), msg.get("source", ""),
                    msg.get("model_version"), msg.get("calibration"),
                    msg.get("n_pixels"), key)
                self.n_obs += 1

    async def run(self):
        import aiomqtt
        await self.start()
        topics = [t.strip() for t in TOPICS.split(",") if t.strip()]
        while True:
            try:
                async with aiomqtt.Client(BROKER, PORT) as client:
                    for t in topics:
                        await client.subscribe(t, qos=1)
                    log.info("subscribed to %s", topics)
                    async for m in client.messages:
                        await self.handle(str(m.topic), m.payload)
            except Exception as e:
                log.warning("broker connection lost (%s); retrying in 5s", e)
                await asyncio.sleep(5)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ing = Ingest()
    loop = asyncio.new_event_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, loop.stop)
    loop.create_task(ing.run())
    loop.run_forever()


if __name__ == "__main__":
    main()
