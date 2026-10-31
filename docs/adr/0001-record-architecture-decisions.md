# 0001 — Record architecture decisions

**Status:** accepted · **Date:** 2026-09-09

## Context

This project has an unusual history: a written architecture existed, in detail,
before any code. When that architecture was checked against primary sources —
SDK support matrices, licence files, PyPI release dates, actual benchmarks —
several of its central choices turned out to be wrong. Not vague, *wrong*: they
named libraries that are archived, algorithms that do not fit the problem, and
hardware that has been discontinued.

That is a normal outcome of designing on paper, and the useful response is not
to quietly fix it. It is to record what changed and why, so the next person does
not re-derive the same mistake from the same plausible reasoning.

## Decision

Every load-bearing choice gets an ADR. Where an ADR reverses an earlier written
decision, it says so explicitly and states the evidence, with a citation that
can be checked.

## Consequences

Some of these records are unflattering to the earlier design. That is the point.
An architecture document that only contains its final answers teaches nothing
about which of its answers are fragile.

## Index

| ADR | Decision | Reverses earlier design? |
|-----|----------|--------------------------|
| 0002 | Conformal prediction over MC dropout | no — confirms |
| 0003 | Greedy/beam search over MPPI for scheduling | **yes** |
| 0004 | No neural crop-model surrogate | **yes** |
| 0005 | PCSE/WOFOST as the twin engine | no — confirms |
| 0006 | Append-only versioning instead of CRDTs | no |
| 0007 | PX4 + Gazebo Harmonic, no ROS 2 | narrows |
| 0008 | Hand-assembled data platform; MinIO removed | **yes** |
| 0009 | Permissive-licence-only model and dataset policy | new |
| 0010 | Adopt farmOS/OCSM/ISOXML data models | new |
| 0011 | Feasibility gates before any number is emitted | new |
| 0012 | Conformal sets for detections, with background as a class | new |
| 0013 | Post-audit rebuild: realistic weather, fixed EnKF, weekly loop | **yes** — replaces the poster's headline numbers |
| 0014 | Gate on actions (crop/weed), class-conditional, field-recalibrated | narrows 0012 |
| 0015 | Global virtual test farm from real data; real archived ensemble forecasts | supersedes India-only defaults of 0013 |
| 0016 | Digital twin of the 10 L K3-A Pro sprayer, calibrated to its spec sheet | new |
