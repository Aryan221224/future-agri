# 0003 — Use greedy/beam search, not MPPI, for agronomic scheduling

**Status:** accepted · **Date:** 2026-09-09 · **Reverses:** the technical annex's
"MPPI receding-horizon search over a fast crop surrogate"

## Context

The earlier architecture specified MPPI (model predictive path integral control)
for choosing interventions, replacing an even earlier MCTS proposal. MPPI was
chosen because MCTS is wrong on continuous action spaces — which is true — and
MPPI is the standard sampling-MPC answer for continuous control.

The problem is that agronomic scheduling is not continuous control.

## Analysis

MPPI's update rule is a softmax-weighted **arithmetic mean of sampled control
sequences**. That works in robotics because the average of two good steering
angles is a good steering angle. Our decision is "apply 0/30/60/120 kg N/ha in
week *k*, or nothing", over ~5–20 weekly epochs of which nearly all are
"nothing". Three mismatches follow:

1. The mean of *urea* and *no application* is not an action. Relaxing to a
   simplex and rounding at the end breaks the importance-sampling weights the
   derivation rests on.
2. Gaussian perturbation around a nominal sequence is a poor proposal for a
   sparse optimum.
3. MPPI's exponential weighting presumes the rollout model is right and only the
   control is noisy. Our dominant error is crop-model structural error and
   weather — a *scenario* problem, not a control-noise problem.

There is also no real-time constraint to justify an anytime sampling method:
roughly 20 decisions per season, not 20 per second.

## Evidence

`bench/planner_comparison.py`, run on the real WOFOST model with an N-limited
paddy scenario, 5 weather scenarios, CVaR-80 objective:

| method | CVaR (INR/ha) | rollouts | plan found |
|--------|--------------:|---------:|------------|
| do-nothing | 7 265 | 5 | — |
| uniform-split (naive heuristic) | **3 484** | 5 | 4 × 30 kg N |
| greedy | **9 034** | **65** | 1 × 30 kg N, Jul 5 |
| beam-3 | 9 034 | 155 | same |
| beam-5 | 9 034 | 230 | same |
| MPPI | 9 034 | 155 | same |

Three findings, one of which contradicts the argument above:

* **MPPI did not lose.** It found the same optimum for a comparable rollout
  budget. The structural objections stand, but at this problem size they did not
  bite. Recording this because the tempting move was to declare victory.
* **Search width bought nothing.** Greedy matched beam-5 using 3.5× fewer
  rollouts. The reason is that the objective is close to separable at this
  horizon — the interaction between application dates is weak relative to the
  "how much N in total" effect.
* **The naive agronomic heuristic is worse than doing nothing** (CVaR 3 484 vs
  7 265) *despite a higher mean margin* (17 752 vs 7 780). Four passes of fixed
  cost paid in every scenario, against a yield gain that fails in the bad ones.

## Decision

Ship **greedy** as the production scheduler. Keep **beam search** for the ranked
alternatives an agronomist wants to see. Keep the MPPI implementation as a
reference and a benchmark control. Optimise **CVaR-80**, not the mean.

Revisit if the action space grows interactions greedy cannot see — e.g. joint
irrigation × fertilisation × spray timing with carry-over effects. At that point
the right escalation is **scenario-based MILP via OR-Tools CP-SAT**, which we
already depend on for routing, not a sampling method.

## Consequences

The planner is explainable line by line, which matters more than it sounds: a
farm manager will not act on a recommendation whose derivation cannot be shown,
and "we sampled 800 Gaussian perturbations and took a softmax average" is not a
derivation anyone will act on.
