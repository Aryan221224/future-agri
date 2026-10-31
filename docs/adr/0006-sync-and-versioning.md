# 0006 — Append-only versioning; per-class sync transports

**Status:** accepted · **Date:** 2026-09-09

## The insight

There is no single sync technology for this problem, and most failed offline-
first agtech systems tried to make one mechanism carry all three data classes.
Split by *sync semantics*:

| class | volume | conflicts | mechanism |
|---|---|---|---|
| sensor timeseries | many, tiny | **none** — append-only, immutable | MQTT bridge, QoS 1 |
| imagery / COGs | few, huge | **none** — write-once | rclone (or Syncthing) |
| prescriptions, boundary edits | few, tiny | **real** | append-only versions + human resolution |

The first two are the bulk of the bytes and have no conflicts at all. That is
what makes this tractable. Do not reach for CRDTs to move sensor readings.

## Mutable state: versions, not CRDTs

Never update in place. Insert `(object_id, version_id, parent_version, author,
device_id, created_at, payload)`. Sync becomes append-only replication. Two
versions sharing a `parent_version` is a **detected conflict**, surfaced at
`/conflicts` for a human.

Automerge is mature and would converge correctly — but CRDTs guarantee
*convergence*, not *correctness*. Two agronomists editing one prescription
converge to a document that is half of one rate map and half of another, and
applying it costs real money. A human resolving three conflicts a month beats an
algorithm silently merging three hundred. (`automerge-repo`, the sync layer one
would actually need, is also still alpha.)

Rejected for the same class of reason: **rqlite** requires a Raft quorum, and a
partitioned gateway cannot accept writes — the exact opposite of what a
disconnected gateway must do. **Litestream** is one-way disaster recovery, not
sync — excellent at that, use it, but it cannot merge. **Postgres logical
replication** accumulates WAL on the publisher when a subscriber is gone, so a
gateway offline for a week can fill the *cloud's* disk.

## The four failure modes this is designed against

1. **Silent queue overflow.** Mosquitto's `max_queued_messages` default of 1000
   is ~12 minutes of buffer for 20 sensors × 4 metrics/minute. An overnight
   outage discards most of a night's readings with no error. Config caps by
   *bytes* instead (`deploy/mosquitto.conf`).
2. **Non-idempotent ingest.** QoS 1 redelivers on reconnect. Every insert is
   `ON CONFLICT DO NOTHING` against a natural key.
3. **Clock skew.** A gateway whose RTC resets stamps an outage into 1970.
   Readings carry a monotonic `seq` alongside wall-clock time so it is
   detectable and repairable.
4. **The continuous-aggregate trap.** Backfilled rows older than a continuous
   aggregate's `start_offset` land *behind* the refresh window and never enter
   the rollup — the dashboard then shows a clean, wrong, silently-incomplete
   chart. `start_offset` is 30 days, sized to a realistic worst-case outage, and
   documented in the migration. Nothing warns you about this one.
