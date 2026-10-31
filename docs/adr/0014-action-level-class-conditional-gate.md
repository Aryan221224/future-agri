# 0014 — Gate on actions, per class, recalibrated in the field

**Status:** accepted · **Date:** 2026-10-31 · narrows [0012](0012-detection-conformal.md)

## Context

The first real detector (RF-DETR-Medium on WeedsGalore, H200, 30 Oct 2026)
gave a *marginal* LAC gate that met its target, 0.923 coverage at α = 0.10, and
still failed where it matters:

* quickweed coverage was **0.52** (n = 27) at α = 0.05; other weeds 0.82. A
  90 % average hid a weed the gate was wrong about half the time.
* **59.7 %** of detections went to human review at α = 0.10. A queue that size
  is not reviewed, so in practice it is an unguarded system.
* calibrating on early dates gave 0.836–0.876 coverage on later dates.
* the detector is German maize at ~2.5 mm resolution; the target is an Indian
  potato or wheat field. Exchangeability between the two is not a defensible
  assumption.

## Decision

1. **Class-conditional (Mondrian) conformal** —
   `fa_core.uncertainty.calibrate_class_conditional`. One quantile per true
   class, so each class has its own guarantee. A class with too few
   calibration examples for its α gets quantile 1.0: it is in every set and
   goes to review (`undercalibrated` names it). No fake guarantee.
2. **Gate on the action, not the species** — `ActionGate`. Softmax mass is
   summed into crop / weed / background before calibrating. Amaranth vs
   quickweed does not change a spray decision; crop vs weed does. The edge
   pipeline routes on the action set when an `ActionGate` is configured.
3. **Recalibrate on field labels** — the review queue (`/twin/{field}/review`)
   collects labels; `recalibrate_with_field_labels` refits once there are ≥ 100
   (in simulation 100–200 field labels restored coverage from 0.63–0.80 to
   ~0.91). Below that the current gate is kept and the report says so.

## Evidence so far

* Synthetic (tests): with a rare weed confused with a common one, the marginal
  gate covered 0.90 overall and **0.27** on the rare class; class-conditional
  covered **≥ 0.86 on every class** (0.903 mean over 40 seeds); the action gate
  cut the review rate further at the same coverage.
* Real scores: `ml/gate_eval.py` runs all three gates on the cached H200
  predictions. Results go to `ml/results/wg-medium/gates.json` and replace the
  simulated numbers above once run.

## Consequences

Class-conditional sets are larger for poorly separated classes — that is the
honest price of a per-class guarantee. The action gate recovers most of it.
The species head is still reported (counts per class), but no automated action
depends on it.
