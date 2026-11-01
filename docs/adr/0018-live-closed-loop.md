# 0018 — Live closed loop: the sprayer's camera corrects the job in flight

**Status:** accepted (simulation) · **Date:** 2026-11-01 ·
**Design:** [spec](../superpowers/specs/2026-11-01-live-closed-loop-design.md) ·
[plan](../superpowers/plans/2026-11-01-live-closed-loop-twin.md)

## Context

The camera rides on the spraying drone, with whatever companion computer and camera
the operator uses. The system has to be a closed loop. It reports live what it sees, and
the twin compares that with what it predicted for the job. It then improves the run that is
still in the air, not only the next one.

## Decision

* **Prediction frozen before take-off** (`fa_runner.closedloop.prediction`). It covers:
  * per-cell weed density and spread from the twin's map;
  * canopy cover;
  * the targets and the prescribed rate;
  * the drone twin's planned dose per cell, sorties, litres and time.
* **Deviation, not just data.**
  * *Weeds:* z = (observed − predicted) / √(sd²pred + sd²obs). The observation sd comes
    from the measured detector recall and false positives at the flight's GSD (H200 sweep).
  * *Dose:* as-applied vs prescribed rate.
  * Only |z| > 2 (dose < 90 % of the rate) over at least 5 cells and 5 % of the cells
    seen is a deviation.
  * Unseen cells are never scored, and they keep the prior in the twin
    (`ScanObservation.observed`).
* **Three loops.**
  * *In pass:* an optional spot valve opens only where the camera sees weeds above the
    threshold, or did not see at all (fail-safe). It never gives a cell more than the plan.
  * *Between sorties:* the planner's own herbicide rule re-runs on the updated map. Cells at
    ≥ 90 % of the rate drop out and under-dosed cells come back, at most twice each.
  * *After the job:* what the camera saw is assimilated as a partial scan.
* **One session action** (`SeasonSession.fly_closed_loop`). It is logged and replays
  identically, and it takes the same weather draw and seed as an open-loop flight, so the
  two differ only by the loop.
* **The aircraft speaks `fa.live/1`** (`fa_core.live_msgs`). Batches carry per-cell counts
  and telemetry, never imagery:
  * each batch is idempotent by `(run_id, seq)` and authenticated by device token;
  * bad items are rejected one by one.
  * Nothing is sent to the aircraft: the next sortie is a KML for its app, and the
    operator can freeze the plan (open loop) at any time.
  * Every correction is in the audit log with its reason.
* **Simulated sprayer camera** (`fa_scan.sprayer_camera`). It uses the same detector model
  as a survey scan, along the drone twin's flown track. This is what lets the loop be tested
  and benchmarked without hardware.

## Measured result (simulation)

`python ml/closed_loop_bench.py --templates all --seeds 0-2`: the twin's own policy runs
until its first herbicide job, then the season is forked three ways on that job's first
flyable day. Results: [`ml/results/closed_loop/closed_loop.md`](../../ml/results/closed_loop/closed_loop.md).

Of seven weedy templates:
* three proposed a herbicide job (A1 rice, A5 potato, A6 maize); 9 farm-seeds in total;
* A2, A3 and A4 proposed none at their weed levels;
* A8 cannot be simulated (no defoliation model).

Paired differences vs the open-loop flight of the same job (mean and 95 % interval):

| | Δ product | Δ weeds 7 days after | Δ yield | Δ net per ha |
|---|---|---|---|---|
| closed loop | **+40 %** [+30, +51] | **−25 %** [−33, −18] | +0.02 % [+0.01, +0.03] | **−0.3 %** [−0.6, −0.1] |
| closed loop + spot valve | +39 % [+29, +49] | −24 % [−31, −17] | +0.01 % [−0.01, +0.04] | −0.3 % [−0.6, −0.1] |

**What this says:**

* The closed loop does what it is built to do: it finds the cells the single pass left under
  90 % of the rate (often 30–45 % of the targets in the drone twin's pattern), re-flies them,
  and leaves a quarter fewer weeds.
* At these weed levels, and after this point in the critical period, the extra kill does not
  show in yield. The extra product and flight time cost slightly more than they return, so
  net per ha is a little lower in every template.
* The spec hoped for less product *and* fewer weeds. Fewer weeds is measured; **less product
  is not**.
* The spot valve adds almost nothing on spot jobs, because the targets already are the cells
  above the threshold. It would matter on a blanket job, or when the survey map is old.

## Consequences

* Ship the loop, but **do not quote a product saving**. Quote the weed reduction with its
  interval and the cost, and say it comes from simulation.
* The fixed 90 % re-fly rule is the next thing to decide on value, not on coverage: re-fly a
  cell only when the expected yield gain at its weed density pays for the product and time.
  The benchmark is the tool for that.
* The full run on the H200 (`ml/h200/closed_loop_bench.sh`, 20 seeds) replaces this table
  when it is done. Templates that propose no herbicide job need a weedier variant to be
  informative.

## Limits

* Simulated camera: recall and false positives come from the detector's measured curves,
  but its footprint, height and timing are assumed (a 1080p, 70° camera at spray height).
* Each sortie is the drone twin's simulation of the job restricted to what the first sortie
  of the remainder reaches. That is close to, but not the same as, the aircraft's own split.
* Fungicide is a field-level decision in the twin: its re-plan only tracks coverage, and the
  valve is herbicide only.
* For aircraft runs, dose from telemetry is coarse (the rate once per pass under a spraying
  segment; flow is not used yet).

## What to measure on real hardware

* Detection + valve latency against look-ahead at the flown speed.
* Recall and false positives of the sprayer camera at spray height, on labelled frames from
  the target crop.
* Cross-track error and as-applied dose from flow logs vs the drone twin.
* The same open / closed comparison on paired strips of a real field.
