# 0008 — Hand-assembled data platform; MinIO removed

**Status:** accepted · **Date:** 2026-09-09 · **Reverses:** the technical annex's
"eoAPI bundle" and "MinIO/S3" object store

## MinIO is gone, and not because of its licence

The earlier design specified MinIO. Current facts:

* 2021 — relicensed Apache-2.0 → AGPLv3.
* May 2025 — breaking release strips the community edition: embedded console
  deprecated, LDAP/OIDC login removed to the commercial product.
* Oct 2025 — last release.
* Late 2025 — community edition in maintenance mode; **pre-built binaries and
  public Docker images discontinued**, source only.
* **2026-04-25 — the repository was archived by its owner.** README reads
  `THIS REPOSITORY IS NO LONGER MAINTAINED`.

AGPL was survivable for an internal service. Archived, binary-less and with no
security-patch commitment is not. Anyone recommending MinIO is working from
pre-2025 knowledge.

**Replacement:** plain filesystem + `rclone` at the field edge (fewer daemons in
the least serviceable location, and GDAL reads local paths faster than S3);
**Garage** (AGPL-3.0, ~200 MB RAM, built for small geo-distributed deployments)
or **SeaweedFS** (Apache-2.0) centrally. Write against the S3 API via boto3 so
the store is swappable, and do not design around versioning or lifecycle
policies — neither Garage nor SeaweedFS has them.

## eoAPI as reference, not dependency

eoAPI is a good reference architecture and its compose file is worth copying.
But its own pins lag its own components by up to two major versions
(titiler-pgstac 1.7.2 against 3.1.0), and its README concedes the Python
components have incompatible dependencies. Adopting the bundle means adopting a
stale pin-set on someone else's schedule.

We hand-assemble, one container per service. The version matrix disappears
because each image ships its own resolved environment — which is exactly why
eoAPI itself ships four containers and not one. **Never** put stac-fastapi,
titiler-pgstac and tipg in one Python environment.

## Broker: Mosquitto, not EMQX

EMQX adopted **BSL 1.1 at version 5.9**, in a minor release. Free production use
is single-node only; clustering and embedding in a commercially distributed
product require a licence — and shipping a field-edge gateway appliance is
arguably exactly that. **EMQX 5.8, the last Apache-2.0 release, reached EOL on
2026-02-28.** There is no maintained Apache-2.0 EMQX.

NanoMQ (MIT) is the better *technical* fit for a constrained edge box, but it is
still 0.x and its recent releases fix bugs in the SQLite persistence path — the
exact store-and-forward durability we would be depending on.

Mosquitto: EPL-2.0/EDL-1.0, Eclipse Foundation governance rather than
single-vendor (both alternatives are EMQ properties), and 2.1 added a
`persist-sqlite` plugin and better bridge controls.

## Database

`timescale/timescaledb-ha:pg17` bundles PostGIS, so the catalog, entities and
hypertables share one instance — a real saving at the edge. Note that
**continuous aggregates and compression are TSL, not Apache-2.0**: the "Apache
edition" of TimescaleDB lacks both and is close to useless for a timeseries
workload. TSL permits internal use and value-added products where the database
is not the offering; it forbids selling hosted database access. We are inside
that line. Never flip editions on a database with compressed chunks — the
documented failure mode returns partial rows rather than erroring.

## Eclipse Ditto rejected as the twin state store

The obvious candidate, and wrong here for a concrete reason rather than a
stylistic one: **Ditto's RQL has no geospatial operators at all** — no polygon,
bounding-box or proximity queries. A system whose primary query is "which twins
are inside this field boundary" cannot use it. It is also a device-shadow model
(our primary entities are fields, crops and seasons, which have geometry and
temporal extent), it is not a timeseries store, and it needs five JVM
microservices plus MongoDB to boot.

What we took instead: its `properties` vs `desiredProperties` split — commanded
state versus reported state — which is the right model for "prescription says
200 L/ha" against "applied 187 L/ha". About 300 lines over PostGIS.
