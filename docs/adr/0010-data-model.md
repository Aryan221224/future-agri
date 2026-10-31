# 0010 — Adopt farmOS's model, OCSM's hierarchy, ISOXML for interchange

**Status:** accepted · **Date:** 2026-09-09

## Decision

Three layers, adopted rather than invented:

1. **Internal model — farmOS's Asset / Log / Quantity**, reimplemented natively
   over PostGIS (not Drupal). The load-bearing ideas: the Asset/Log split; the
   distinction between what a log happened **to** (assets) and **in**
   (locations); `status` of pending/done/abandoned, which gives planned-versus-
   executed missions for free; geometry on every log; revisioning of everything.
   farmOS is the only open agricultural record model stress-tested by real farms
   across livestock, row crop and research use.

2. **Land hierarchy — OCSM's Farm → Site → Parcel → ManagementZone**, with
   observations shaped like OGC SensorThings (Thing/Datastream/Observation).
   ManagementZone is the prescription unit.

3. **External interchange — ISO 11783-10 (ISOXML / TASKDATA)** for prescriptions
   and as-applied data: `PFD` boundaries, `TZN` + `PDV` treatment zones, Grid for
   raster prescriptions, `TLG` for as-applied logs.

Plus **content-addressed field identity** (`fa_core.geoid`), AgStack's S2-cover
idea implemented directly: stable under vertex reordering, ring winding, CRS
round-trips and sub-metre jitter, so one field digitised twice by two people
resolves to one record.

## Why models were adopted but implementations were not

Every candidate implementation fails on stack or licence, not on design:
farmOS is Drupal/PHP; FROST-Server (OGC SensorThings) is a JVM service that
would duplicate our TimescaleDB hypertables and lose compression and continuous
aggregates; AgGateway ADAPT is .NET-only (EPL-1.0); FIWARE Smart Data Models
drag in NGSI-LD and the Orion context broker. Take the vocabulary, write the
schema in an afternoon, and keep the option of putting a conformant read API in
front of our own tables if a customer ever demands one.

## ISOXML is a day-one feature, not a roadmap item

This is the one that decides whether the product is usable. Interoperability
burden currently falls on the farmer — it is up to them to work out whether a
new implement talks to their existing system — and that burden kills deals. If a
prescription cannot load into the controller a grower already owns, there is no
product. Do not invent a prescription format; ISOXML is what equipment and
agronomists accept.

## The failure modes this data model is shaped against

From the adoption literature, and each one is answered by a specific choice
above:

* Precision-ag adoption has been **systematically overestimated for 20 years
  across 26 technologies**, with UAV imagery services explicitly named as an
  underperformer — dealer capacity *declined* despite the hype.
* **~50 % of farmers globally will not pay** for advisory software at all; those
  who will demand roughly **3:1 ROI**. Hence `Economics` and a CVaR objective
  denominated in currency per hectare — the system must state its own economic
  effect as a first-class output, not a dashboard decoration.
* **Ag drone spraying prices fell 38 % in one year** while acreage grew 59 %.
  The durable value is fleet *utilisation and scheduling*, not another mapping
  tool. Hence energy-constrained multi-sortie routing as a core component.
* **Connectivity and rural infrastructure**, not sensing or autonomy, were the
  binding barriers in the largest EU drone-agriculture programme. Hence
  offline-first as an architectural invariant rather than a feature.
* **Digital twins fail validation across seasons** — most published evaluations
  cover one or a few. Hence model versioning on every recommendation, and ADR
  0011's refusal to emit numbers the model cannot support.
* **Low-CAPEX digital agtech dies in 2–6 years when adoption stalls**, faster
  than hardware ventures. Being software-light buys a shorter runway, not a
  longer one.
