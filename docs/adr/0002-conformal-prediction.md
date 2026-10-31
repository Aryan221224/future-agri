# 0002 — Split conformal prediction for uncertainty, not MC dropout

**Status:** accepted · **Date:** 2026-09-09

## Context

Findings from an edge vision model must be routed either to an automated
decision or to a human. That routing needs a *calibrated* notion of confidence.
A softmax score is not one.

## Decision

Split conformal prediction, implemented natively in `fa_core.uncertainty`:

* **classification** → prediction *sets*; a set that is not a singleton goes to
  human review;
* **regression** (LAI, canopy cover) → intervals, with optional normalisation by
  a per-sample difficulty estimate;
* **stratified** by (crop, growth stage, site), with an explicit global fallback
  that also marks the observation for review.

## Why not MC dropout

MC dropout needs N stochastic forward passes per image (N ≈ 30 typical) and
still provides no coverage guarantee. Split conformal needs one forward pass and
a held-out calibration set, and provides a distribution-free finite-sample
guarantee under exchangeability. On an edge budget where inference is the
binding constraint, paying ~30× inference for a weaker guarantee is a bad trade.

## Why not a library

MAPIE (BSD-3) and puncc (MIT) are both good and either can be swapped in. We
implemented natively because it is ~150 lines of NumPy, it keeps the edge
container thin, and it lets stratification and the coverage monitor be
first-class rather than bolted on. Two libraries were actively rejected:

* **TorchCP** — the most-cited library in this space, and **LGPL-3.0/GPL-3.0**,
  *and* it does not support object detection at all despite its reputation.
* **CODS** — the only correct conformal-object-detection implementation, MIT,
  but it hard-depends on `ultralytics` (AGPL-3.0) purely for model wrappers.
  Installing it drags AGPL in sideways. Vendor the scoring code, drop the dep.

## Measured behaviour

6 classes, 4 000 calibration / 20 000 test samples, α = 0.10:

| score | mode | coverage | mean set size | review rate |
|-------|------|---------:|--------------:|------------:|
| LAC | deterministic | 0.9058 | 2.43 | 78.1 % |
| LAC | randomized | 0.9058 | 2.43 | 78.1 % |
| RAPS | deterministic | 0.9286 | 2.96 | 96.0 % |
| RAPS | randomized | 0.9074 | 2.66 | 92.8 % |

Regression, α = 0.10: coverage 0.8988 constant-width; 0.9088 normalised, with
mean width 0.928 against 0.952 — same guarantee, adaptive intervals.

Two things fell out of this that were not obvious in advance:

1. **Deterministic RAPS over-covers** (0.929 against 0.90). Over-coverage is not
   free: it inflates set sizes and therefore the human review rate, which is the
   operational cost that decides whether anyone keeps using the system.
2. **LAC is the production default.** Near-exact deterministically, smallest
   sets, lowest review load. Randomization would buy exactness for RAPS at the
   cost of the same image producing different sets on different runs — and a
   system that must answer "why was this flagged?" months later needs
   reproducibility more than it needs the last 3 % of set size.

## The caveat that matters most

Agriculture violates exchangeability harder than almost any domain: lighting,
growth stage, cultivar, soil background, season. A single global calibration
gives nominal coverage on paper and materially less in a new field. Therefore:

* calibrate per (crop × stage × site), ≥ 200 samples per stratum, and fall back
  loudly, never silently;
* treat empirical coverage as a **monitored production metric with an alarm**
  (`/coverage`), not a validation-time number;
* recalibrate every new field and season.

The humans working the review queue are also, for free, producing the labels
that keep calibration honest. That feedback loop is the reason the review queue
is a first-class part of the architecture rather than a fallback.
