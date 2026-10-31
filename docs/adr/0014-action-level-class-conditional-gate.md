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
* **Real scores** (31 Oct 2026, `ml/gate_eval.py` on the cached H200 predictions:
  1,839 validation / 1,472 test detections; the marginal row reproduces the H200
  run exactly, which validates the pipeline; results in
  `bench/results/gates_wg_medium.json`):

  | α = 0.10 | coverage (95 % CI) | worst class | review rate |
  |---|---|---|---|
  | marginal LAC | 0.923 (0.901–0.946) | quickweed **0.37** (n = 27) | 59.7 % |
  | class-conditional | 0.915 (0.895–0.933) | quickweed **0.81** | 70.7 % |
  | action level (crop/weed/bg) | 0.859 (0.826–0.890) | weed **0.82** | **38.5 %** |

  At α = 0.05: marginal 0.956 / worst 0.52 / review 77.2 %; class-conditional
  0.948 / 0.81 / 78.9 %; action 0.920 / 0.895 / 58.2 %.

## What the real scores changed

* **Class-conditional does what it is for**: the rare weed's coverage goes from
  0.37 to 0.81 (n = 27, so ± ~0.15), weed_other from 0.76 to 0.87, overall on
  target — at the cost of more review (59.7 % → 70.7 %).
* **The action gate cut review by a third, not to 2.5 %.** The simulated 78.7 % →
  2.5 % was far too optimistic. And it **under-covers on test** (0.86 vs 0.90):
  its pooled "weed" group mixes four species whose scores shift between the
  validation and test dates. Projecting class-conditional sets onto actions was
  also tried: it over-covers (0.971) and reviews 62.8 %, no better than marginal,
  so it was not adopted.
* **On this detector the gates trade coverage against review; none gives both.**
  The detector scores 0.11–0.25 AP50 on three of four weed classes. The review
  rate will move with a better detector and with field data, not with a cleverer
  gate. Use class-conditional where a guarantee is needed (species reporting),
  the action gate for spray routing **only after recalibrating it on field labels
  stratified by date**, and treat both as unvalidated for an Indian field.

## Consequences

Class-conditional sets are larger for poorly separated classes — that is the
honest price of a per-class guarantee. The action gate recovers most of it.
The species head is still reported (counts per class), but no automated action
depends on it.
