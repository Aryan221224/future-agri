# 0004 — Do not build a neural surrogate of the crop model

**Status:** accepted · **Date:** 2026-09-09 · **Reverses:** the technical annex's
"neural crop-model surrogate" for planning rollouts

## Context

The earlier architecture specified training a neural emulator of the crop model
so the planner could afford thousands of forward simulations. This mirrors a
real and active literature — APSIM emulation with MLPs reaching R² ≈ 0.93 across
13 outputs, "several orders of magnitude" faster than the simulator.

## The number that settles it

Measured on this stack, this machine, single core:

```
PCSE / WOFOST 7.2, 150-day water-limited rice season:  109–153 ms
                                        ≈ 6.6–9 seasons / second / core
                       100-member ensemble:  ~15 s single-core, ~2 s on 8 cores
```

The surrogate literature exists because **APSIM takes ~2 minutes per run**. That
~850× gap is what justifies emulation. It does not exist for PCSE.

## Analysis

Building a surrogate would cost:

* 10⁵–10⁶ training simulations (≈ 40 core-hours for 10⁶ at our measured rate) —
  affordable, but it buys a speedup we do not need;
* a permanent, unvalidated approximation sitting between the observations and
  the recommendation, whose failure mode is silent and correlated with exactly
  the unusual seasons where the advice matters most;
* an extra artefact to version, retrain and monitor for drift every time the
  crop parameters change.

Our actual compute budget: the demo runs 160 rollouts in ~25 s. The planner's
binding constraint is scenario count, and scenarios parallelise trivially across
cores.

## Decision

No surrogate. Run the crop model directly. Parallelise across ensemble members
and scenarios with a process pool.

Revisit only if a use case appears that genuinely needs ≥10⁶ rollouts in an
interactive loop — continent-scale sweeps, or gradient-based inverse design
where differentiability itself is the requirement. Neither is on the roadmap,
and no open implementation of a WOFOST surrogate exists to start from.

## Consequences

One fewer model to train, version, monitor and explain. Every number the planner
produces comes from the same physiological model the twin uses, so there is no
possibility of the planner and the twin disagreeing about the same field.
