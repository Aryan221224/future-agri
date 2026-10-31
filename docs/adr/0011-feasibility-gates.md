# 0011 — Refuse to emit a number when the model cannot support it

**Status:** accepted · **Date:** 2026-09-09

## Context

This ADR exists because of a bug found on the first real run of the twin, and it
generalises into the single most important safety property of the system.

Configuring WOFOST for winter wheat at Ludhiana (30.9° N) — a completely
plausible thing for a user to do, in one of the largest wheat regions on earth —
produced:

```
TWSO = 0.0 kg/ha      LAImax = 1.43      DVS = 0.066
```

No exception. No warning. A season of biomass growth simulated, and a yield of
exactly zero.

## Cause

Every wheat cultivar in the WOFOST parameter repository is European and carries
`IDSL=2` (photoperiod- and vernalisation-sensitive) with optimum photoperiod
`DLO` of 16.3–16.8 h. Measured with PCSE's own `daylength()`:

```
longest day at 30.90° N (Punjab)      14.72 h
longest day at 51.97° N (Wageningen)  17.69 h
```

The photoperiod reduction factor never approaches 1, thermal time accumulates
far too slowly, development stalls at DVS 0.07, and no grain is ever filled.

## Why this is the dangerous class of bug

A crash is honest. A zero that looks like a number is not: it flows into a
dashboard, an average, a recommendation. This is precisely the "performance
illusion" the digital-twin critique literature warns about — data assimilation
producing apparent accuracy over a structurally incomplete model.

## Decision

The twin runs **feasibility gates before it is trusted**, and refuses rather
than returns a suspect number:

* `check_photoperiod()` — compares cultivar `DLO`/`DLC` against the maximum
  daylength at the site latitude, and rejects with an explanation naming the
  numbers.
* `variety_support()` — enumerates cultivars an engine can actually run.
* `resolve_start_type()` — detects transplanted crops (`TSUMEM = 0`).
* `UNCALIBRATED` — crops whose parameters are generic literature placeholders
  carry a warning into every summary they produce.

The demo runs this gate visibly, and it rejects wheat-at-Punjab every time.

## Generalisation

Any component that can produce a plausible-looking number from an invalid
configuration owes a gate:

* the router independently re-verifies every produced plan's energy feasibility
  rather than trusting the solver's own accounting (`routing.verify`);
* the conformal layer refuses to certify a stratum with too few calibration
  samples and falls back loudly (`StratifiedCalibrator.get` returns a hit flag);
* the EnKF rejects observations more than 5σ from the whole ensemble — far more
  likely a cloud, a shadow or a bad retrieval than a real crop state;
* synthetic weather stamps `NOT-FOR-ADVICE` into every summary it touches.

## Consequence

The system says "I cannot model this" more often than a naive one. That is the
intended behaviour. Every one of these gates converts a silent wrong answer into
a loud, actionable one.
