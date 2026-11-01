# Live Closed Loop (twin and console) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A spray job corrects itself while it is flown. A camera on the sprayer reports per-cell observations. The twin scores them against a prediction frozen before take-off, then re-plans the rest of the job between sorties, and optionally opens a spot valve during a pass. This works end to end on virtual farms, and real messages are accepted for live fields. The benefit is measured, not asserted.

**Architecture:**
- **Pure functions in the runner:** prediction, deviation, sprayer camera, re-plan and the closed-loop flight. These are unit-testable and deterministic from the session seed.
- **Session action:** `SeasonSession.fly_closed_loop` logs its action, so a restored session replays the same run.
- **Console side:** `LiveRunService` (console) holds a run's state for the UI. On a virtual farm it runs as a job, sortie by sortie. On a live field it ingests idempotent message batches from the on-board agent and writes the next sortie's KML.
- **Message schema:** lives in `fa_core` so the on-board agent (plan 2) uses the same one.

**Tech Stack:** Python 3.10+ (numpy, shapely, pyproj, FastAPI, pydantic), React 19 + three.js, pytest + Playwright.

**Spec:** `docs/superpowers/specs/2026-11-01-live-closed-loop-design.md` (approved 1 Nov 2026). This plan implements spec order-of-work steps 1, 2 and 5. Spec steps 3 (on-board agent) and 4 (actuation adapters) are plan 2: `docs/superpowers/plans/<date>-onboard-agent.md`, written after this plan lands. It consumes `fa_core.live_msgs` unchanged.

## Global Constraints

- English only in every UI string, message, report and doc. Global product: no country- or region-specific defaults.
- No login; the console binds to 127.0.0.1 by default. Message ingest is authenticated by device token (reuse `DeviceService` token hashing).
- "Only |z| > 2 over enough cells counts as a deviation". Enough cells means at least 5 cells **and** at least 5 % of the cells observed in that batch or sortie.
- "minus cells already dosed ≥ 90 % of target": a cell is done when its cumulative dose ≥ 0.9 × prescribed rate.
- "Spot spraying can only reduce product relative to the approved plan": per cell, valve dose ≤ the dose the plan would have put there, in every sortie.
- "The agent never commands flight directly". This plan only produces files (KML, settings sheet) and state; nothing is sent to an aircraft.
- "The operator can freeze the plan (open loop) at any time; every correction is in the audit log with its reason."
- Unobserved cells keep the prior. A cell the camera did not cover must never read as zero weeds.
- Every assumed constant states its provenance in code (`# assumed: …`), in the existing style of `simulate_scan`.

## Review Focus

1. **Batch replayed after a link drop** (same `seq` twice, or out of order) → counted once; run state identical to a single delivery. *Test: Task 6, `test_replayed_and_out_of_order_batches_count_once`.*
2. **Camera covered only some of the field** → unobserved cells keep their prior weed density in the twin and in the re-plan. *Test: Task 1, `test_unobserved_cells_keep_prior`.*
3. **Nothing left to spray after a sortie** (all targets dosed, or the re-plan's option is "none") → the run stops cleanly. No zero-target mission is simulated, and nothing crashes on an empty target mask. *Test: Task 5, `test_stops_when_nothing_left`.*
4. **Camera too high for the detector** (recall < 0.05 at the flight GSD) → the observations are uninformative. No deviation is flagged and no re-plan is triggered by weeds. *Test: Task 3, `test_too_coarse_gsd_is_uninformative`.*
5. **Malformed or outside-the-field items** in a batch (NaN lat, cell index out of range, negative counts) → each is rejected with a reason and the rest of the batch is accepted. *Test: Task 6, `test_bad_items_rejected_individually`.*

---

## File Structure

| File | Responsibility |
|---|---|
| `services/scan/fa_scan/measure.py` (modify) | `ScanObservation.observed` mask |
| `services/scan/fa_scan/digital.py` (modify) | zones from observed cells only |
| `services/runner/fa_runner/twinfarm.py` (modify) | ingest a partial scan; `herbicide_targets(weed_map)` |
| `services/runner/fa_runner/closedloop/prediction.py` (create) | frozen `Prediction`, `deviation()` |
| `services/scan/fa_scan/sprayer_camera.py` (create) | `SprayerCamera`, `cells_along()`, `observe()` |
| `services/runner/fa_runner/closedloop/replan.py` (create) | `merge_observations()`, `remaining_targets()` |
| `services/runner/fa_runner/closedloop/run.py` (create) | `fly_closed_loop()` sortie loop, valve, `ClosedLoopResult` |
| `services/runner/fa_runner/session.py` (modify) | `SeasonSession.fly_closed_loop` action |
| `packages/fa_core/fa_core/live_msgs.py` (create) | message schema v1 + `validate_batch()` |
| `services/console/fa_console/live_run.py` (create) | `LiveRunService` (virtual job, live ingest, next-sortie KML) |
| `services/console/fa_console/routes/live_run.py` (create) | `/api/farms/{fid}/live-run…`, `/api/live-run/ingest` |
| `services/console/fa_console/exports.py` (modify) | `targets_kml(fid, name, targets)` |
| `apps/console/src/pages/Mission.tsx`, `apps/console/src/pages/LiveRun.tsx` (create), `apps/console/src/three/FarmScene.ts` (modify), `apps/console/src/api.ts` (modify) | live view |
| `ml/closed_loop_bench.py` (create), `docs/adr/0018-live-closed-loop.md` (create) | benchmark and ADR |

---

### Task 1: Partial-scan assimilation (observation mask)

**Files:**
- Modify: `services/scan/fa_scan/measure.py` (`ScanObservation`)
- Modify: `services/scan/fa_scan/digital.py` (`build_digital_farm`)
- Modify: `services/runner/fa_runner/twinfarm.py` (`TwinFarm.ingest`, new `herbicide_targets`)
- Test: `tests/test_partial_scan.py`

**Interfaces:**
- Produces:
  - `ScanObservation.observed: np.ndarray | None = None` (bool per cell; None = whole field). Added as the **last** field with a default, so existing positional constructors keep working.
  - `TwinFarm.herbicide_targets(weed_map: np.ndarray) -> tuple[str, np.ndarray]` returns the option and a bool target mask.
  - The rule is the one `TwinField.ingest_pass` uses: `herbicide_decision(weed_map, self.field._expected_yield_hint(), self.field.s.econ, self.field.s.weeds)`. Blanket → all True; spot → `weed_map > decision.threshold_m2`; none → all False.

- [ ] **Step 1: Write the failing tests**

```python
def test_digital_farm_zones_use_observed_cells_only(grid, obs_full):
    obs = dataclasses.replace(obs_full, observed=half_mask(grid), cc_cells=np.where(half_mask(grid), 0.6, 0.0))
    df = build_digital_farm(obs, grid)
    cc = sum(z.cc * z.area_ha for z in df.zones) / sum(z.area_ha for z in df.zones)
    assert cc == pytest.approx(0.6, abs=0.02)               # the unobserved 0.0s are not averaged in
    assert np.isnan(df.weed_map[~half_mask(grid)]).all()   # unobserved = unknown, not zero

def test_unobserved_cells_keep_prior(session_after_first_scan):
    s = session_after_first_scan
    prior = s.twin.digital.weed_map.copy()
    obs = partial_obs(s, observed=first_quarter(s.grid), weeds_m2=0.0)  # observed quarter now clean
    s.twin.ingest(build_digital_farm(obs, s.grid), s.day)
    m = first_quarter(s.grid)
    assert np.allclose(s.twin.digital.weed_map[~m], prior[~m])
    assert np.allclose(s.twin.digital.weed_map[m], 0.0, atol=0.5)

def test_herbicide_targets_follow_the_planner_rule(session_after_first_scan):
    tw = session_after_first_scan.twin
    opt, tg = tw.herbicide_targets(np.full(len(tw.digital.weed_map), 0.0))
    assert opt == "none" and not tg.any()
    opt, tg = tw.herbicide_targets(np.full(len(tw.digital.weed_map), 40.0))
    assert opt == "blanket" and tg.all()
```

Fixtures go in the test file: `session_after_first_scan` builds `SeasonSession(fake_vf, seed=1)` on the synthetic farm from `tests/test_console_farms.fake_builder`, then runs until the first `scan` event. `partial_obs` constructs a `ScanObservation` with `observed` set.

- [ ] **Step 2: Run** `pytest tests/test_partial_scan.py -v`. Expected: FAIL (`unexpected keyword 'observed'`).
- [ ] **Step 3: Implement**
  - In `build_digital_farm`: when `obs.observed` is not None, zone quantiles and zone means use only observed cells. Unobserved cells get zone `-1`, which is excluded from `zones`. `weed_map`/`weed_sd` are NaN there.
  - In `TwinFarm.ingest`: merge before deciding. `weed_map = where(isfinite(new), new, previous digital.weed_map if any, else nanmean(new))`. Store the merged map as `self.digital.weed_map`.
- [ ] **Step 4: Run** `pytest tests/test_partial_scan.py tests/test_scan.py tests/test_session*.py -q`. Expected: all pass (full-field scans unchanged).
- [ ] **Step 5: Commit** `feat(twin): partial scans keep the prior on unobserved cells`

---

### Task 2: Frozen prediction and deviation scoring

**Files:**
- Create: `services/runner/fa_runner/closedloop/__init__.py`, `services/runner/fa_runner/closedloop/prediction.py`
- Test: `tests/test_closedloop_prediction.py`

**Interfaces:**
- Consumes: `SeasonSession` (`.twin`, `.grid`, `.flight`, `.spec`); `Operation` (`.product`, `.targets`, `.settings`); `simulate_mission`.
- Produces:
  - `@dataclass Prediction` with these fields:
    - `op_id: str`, `product: str`
    - `weeds_mean: np.ndarray`, `weeds_sd: np.ndarray` (per cell; sd = inf where the twin has no map)
    - `cc_mean: float`, `cc_sd: float`
    - `targets: np.ndarray` (bool)
    - `dose_l_ha: np.ndarray` (planned per cell)
    - `sorties: int`, `time_min: float`, `litres: float`
    - `made_at: str`
    - `to_json() -> dict`
  - `freeze_prediction(session, op) -> Prediction`. It runs `simulate_mission` once with the op's settings and targets (same seed rule as `_fly`), and takes the weed map and sd from `session.twin.digital`.
    - `cc_mean = 1 - exp(-K_COVER * twin LAI)`.
    - `cc_sd = 0.08` (`# assumed: twin canopy spread`).
  - `@dataclass Deviation` with fields `channel: str` ("weeds" | "canopy" | "dose" | "track"), `cells: np.ndarray` (int indices), `z_or_ratio: np.ndarray`, `significant: bool`, `summary: str`, `correction: str`.
  - `deviation_weeds(pred, obs_est, obs_sd, observed) -> Deviation`. Here `z = (obs - mean) / sqrt(sd² + obs_sd²)` over observed cells with finite terms. The enough-cells rule is in Global Constraints. `correction` is `"re-plan remaining targets"` when significant, else `""`.
  - `deviation_dose(pred, dose_cum, flown) -> Deviation`. It uses `ratio = dose_cum / pred.dose_l_ha` on flown target cells. Significant when cells with ratio < 0.9 meet the enough-cells rule. `correction` is `"re-fly under-dosed cells"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_weed_z_score_flags_only_real_deviation():
    pred = _pred(weeds_mean=np.full(100, 10.0), weeds_sd=np.full(100, 2.0))
    obs = np.full(100, 10.5); obs[:20] = 30.0
    d = deviation_weeds(pred, obs, np.full(100, 1.0), np.ones(100, bool))
    assert d.significant and set(d.cells) == set(range(20)) and "re-plan" in d.correction

def test_noise_is_not_chased():
    pred = _pred(weeds_mean=np.full(100, 10.0), weeds_sd=np.full(100, 2.0))
    obs = 10 + np.random.default_rng(0).normal(0, 1.5, 100)
    assert not deviation_weeds(pred, obs, np.full(100, 1.0), np.ones(100, bool)).significant

def test_few_cells_are_not_enough():
    pred = _pred(weeds_mean=np.full(1000, 10.0), weeds_sd=np.full(1000, 1.0))
    obs = np.full(1000, 10.0); obs[:4] = 50.0                   # 4 cells < 5
    assert not deviation_weeds(pred, obs, np.ones(1000), np.ones(1000, bool)).significant

def test_no_map_means_no_deviation():
    pred = _pred(weeds_mean=np.zeros(50), weeds_sd=np.full(50, np.inf))
    assert not deviation_weeds(pred, np.full(50, 99.0), np.ones(50), np.ones(50, bool)).significant

def test_underdosed_flown_cells_are_a_dose_deviation():
    pred = _pred(targets=np.ones(100, bool), dose_l_ha=np.full(100, 20.0))
    dose = np.full(100, 20.0); dose[:10] = 8.0
    d = deviation_dose(pred, dose, flown=np.ones(100, bool))
    assert d.significant and set(d.cells) == set(range(10))

def test_freeze_prediction_on_a_virtual_farm(session_with_herbicide_op):
    s, op = session_with_herbicide_op
    p = freeze_prediction(s, op)
    assert p.litres > 0 and p.sorties >= 1 and len(p.weeds_mean) == s.grid.n_cells
    assert json.loads(json.dumps(p.to_json()))["op_id"] == op.id
```

- [ ] **Step 2: Run** `pytest tests/test_closedloop_prediction.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `prediction.py` with the signatures above.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(closed-loop): frozen prediction and deviation scoring`

---

### Task 3: Simulated sprayer camera

**Files:**
- Create: `services/scan/fa_scan/sprayer_camera.py`
- Test: `tests/test_sprayer_camera.py`

**Interfaces:**
- Consumes: `CellState` (`weeds_m2`, `cc`, `blight_sev`); `DetectorModel` (`weed_recall`, `fp_per_m2`); `MissionResult.track` keyframes `{t, x, y, z, spraying, sortie, …}` in grid-local metres; `grid.cells_xy`, `grid.cell_m`.
- Produces:
  - `@dataclass SprayerCamera` with fields `hfov_deg: float = 70.0`, `width_px: int = 1920`, `lookahead_m: float = 2.0`, `name: str = "generic 1080p, 70° HFOV"`. Defaults are `# assumed: a typical sprayer FPV/survey camera; replace with the calibration file`.
    - `footprint_m(height_m) -> float` returns `2·h·tan(hfov/2)`.
    - `gsd_m(height_m) -> float` returns `footprint_m / width_px`.
  - `cells_along(track: list[dict], grid, width_m: float, sortie: int | None = None, spraying_only: bool = True) -> np.ndarray` returns a bool mask of cells whose centre lies within `width_m/2` of any track segment, filtered by sortie and spray state. Use shapely `LineString(...).buffer(width_m/2)` and `contains_xy` on the cell centres.
  - `observe(state, grid, seen: np.ndarray, gsd_m: float, das: int, rng, detector=None) -> ScanObservation` returns a `ScanObservation` with `observed=seen`. It uses the same recall/FP/size model as `simulate_scan`; unseen cells get `weed_density_est = nan` and sd = inf.
  - `to_cell_messages(obs, run_id: str, t: str) -> list[dict]` returns items of the Task 6 schema `cells[]` item shape.

- [ ] **Step 1: Write the failing tests**

```python
def test_footprint_and_gsd():
    cam = SprayerCamera()
    assert cam.footprint_m(3.0) == pytest.approx(4.2, abs=0.05)
    assert cam.gsd_m(3.0) == pytest.approx(0.0022, abs=0.0001)

def test_cells_along_one_pass(grid):
    y = float(grid.cells_xy[:, 1].mean())
    track = [{"x": grid.cells_xy[:, 0].min(), "y": y, "sortie": 1, "spraying": True},
             {"x": grid.cells_xy[:, 0].max(), "y": y, "sortie": 1, "spraying": True}]
    m = cells_along(track, grid, width_m=grid.cell_m)
    assert m.any() and np.all(np.abs(grid.cells_xy[m, 1] - y) <= grid.cell_m)

def test_observe_matches_truth_on_seen_cells_and_nan_elsewhere(grid, state):
    seen = np.zeros(grid.n_cells, bool); seen[: grid.n_cells // 3] = True
    obs = observe(state, grid, seen, gsd_m=0.0025, das=25, rng=np.random.default_rng(0))
    assert np.isnan(obs.weed_density_est[~seen]).all()
    err = np.nanmean(obs.weed_density_est[seen]) - state.weeds_m2[seen].mean()
    assert abs(err) < 0.25 * state.weeds_m2[seen].mean() + 0.5

def test_too_coarse_gsd_is_uninformative(grid, state):
    obs = observe(state, grid, np.ones(grid.n_cells, bool), gsd_m=0.05, das=25, rng=np.random.default_rng(0))
    assert np.isinf(obs.weed_density_sd).all()
```

- [ ] **Step 2: Run** `pytest tests/test_sprayer_camera.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `sprayer_camera.py`. Refactor the shared count model out of `simulate_scan` into `_weed_counts(state_weeds, cell_m2, rec, fp_m2, rng)` in `measure.py`, used by both.
- [ ] **Step 4: Run** `pytest tests/test_sprayer_camera.py tests/test_scan.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(scan): simulated camera on the sprayer`

---

### Task 4: Between-sortie re-plan

**Files:**
- Create: `services/runner/fa_runner/closedloop/replan.py`
- Test: `tests/test_closedloop_replan.py`

**Interfaces:**
- Consumes: `TwinFarm.herbicide_targets` (Task 1), `Prediction` (Task 2).
- Produces:
  - `merge_observations(prior: np.ndarray, est: np.ndarray, sd: np.ndarray, seen: np.ndarray) -> tuple[np.ndarray, np.ndarray]` returns the merged weed map and the cumulative seen mask. Seen cells with finite sd take the observation; when a cell is seen twice, use an inverse-variance mean; unseen cells keep the prior.
  - `remaining_targets(twin, weed_map, dose_cum, rate_l_ha, product) -> tuple[np.ndarray, str]` returns the target mask and a reason string.
    - Herbicide: `twin.herbicide_targets(weed_map)` minus cells with `dose_cum >= 0.9 * rate_l_ha`.
    - Fungicide: the op's original targets minus done cells (fungicide is field-level in the twin).
    - The reason names the option, the cell counts dropped as done, and the counts re-added as skipped.

- [ ] **Step 1: Write the failing tests**

```python
def test_sprayed_cells_drop_out_and_skipped_ones_come_back(twin_spot):
    w = np.full(100, 30.0)                                     # all above threshold
    dose = np.zeros(100); dose[:60] = 20.0; dose[60:70] = 5.0  # 60 done, 10 skipped strips
    tg, why = remaining_targets(twin_spot, w, dose, 20.0, "herbicide")
    assert not tg[:60].any() and tg[60:].all() and "60 cells done" in why

def test_clean_observed_cells_leave_the_plan(twin_spot):
    w = np.full(100, 30.0); w[:50] = 0.0
    tg, _ = remaining_targets(twin_spot, w, np.zeros(100), 20.0, "herbicide")
    assert not tg[:50].any()

def test_merge_keeps_prior_where_unseen():
    prior = np.full(10, 5.0); est = np.full(10, np.nan); est[:3] = 1.0
    seen = np.zeros(10, bool); seen[:3] = True
    m, cum = merge_observations(prior, est, np.full(10, 0.5), seen)
    assert np.allclose(m[:3], 1.0) and np.allclose(m[3:], 5.0) and cum.sum() == 3
```

- [ ] **Step 2: Run** `pytest tests/test_closedloop_replan.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `replan.py`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(closed-loop): re-plan the remaining job between sorties`

---

### Task 5: Closed-loop flight and the session action

**Files:**
- Create: `services/runner/fa_runner/closedloop/run.py`
- Modify: `services/runner/fa_runner/session.py` (new `fly_closed_loop`, a replay entry in `restore`, and `fly_closed_loop` in the action dispatch used by `SessionService._call`)
- Test: `tests/test_closedloop_run.py`

**Interfaces:**
- Consumes: Tasks 2–4; `simulate_mission(..., record_track=True)`; `SeasonSession._account`.
- Produces:
  - `@dataclass SortieReport` with fields:
    - `n: int`, `cells_sprayed: int`, `cells_seen: int`, `litres: float`
    - `deviations: list[Deviation]`, `correction: str`, `remaining: int`
    - `track: list[dict]`, `detections: list[dict]` (cell, est), `targets_next: list[int]`
  - `@dataclass ClosedLoopResult` with fields `prediction: Prediction`, `sorties: list[SortieReport]`, `dose_l_ha: np.ndarray`, `kpis: dict` (same keys as `MissionResult.kpis`, summed), `valve: bool`, `stopped: str`, `audit: list[dict]`.
  - `fly_closed_loop(session, op, valve: bool = False, camera: SprayerCamera | None = None, max_sorties: int = 60, on_sortie=None) -> ClosedLoopResult`. Algorithm per iteration:
    1. Simulate the mission for the current targets.
    2. Keep only its first sortie: cells = `cells_along(track, grid, swath_m, sortie=1)`, dose = `dose_l_ha * cells`.
    3. If `valve` is set, multiply the dose by the open mask: seen & `est > twin threshold`, or not seen (fail-safe: spray as planned).
    4. The camera observes `cells_along(track, grid, cam.footprint_m(h), sortie=1, spraying_only=False)`.
    5. Merge the observations, score the deviations, re-plan, and call `on_sortie(report)`.
    6. Repeat until the targets are empty (`stopped="done"`), there is no progress (`"no progress"`), or `max_sorties` is reached.
    - `dose_cum` is kept per cell as the sum over sorties.
    - Seeds: `session.seed * 97 + len(session.missions_log) + 1000 * k` for sortie k.
  - `SeasonSession.fly_closed_loop(op_id: str, valve: bool = False) -> dict`. It mirrors `fly_now`: the same checks and the weather-window check, `_log("fly_closed_loop", {...})`, then `_account(d, op, flight, res.kpis, res.dose_l_ha, warnings, track)`, and stores `self.closed_loop[op_id] = res` (summary only). It returns `{"flown", "reason", "op", "closed_loop": summary}`. Replaying the logged action in `restore` reproduces the result bit for bit.

- [ ] **Step 1: Write the failing tests**

```python
def test_closed_loop_uses_less_or_equal_product_than_open(session_with_herbicide_op):
    s_open, op = session_with_herbicide_op
    s_closed = clone(s_open)                         # SeasonSession.restore(kwargs, actions)
    s_open.fly_now(op.id)
    out = s_closed.fly_closed_loop(op.id)
    assert out["flown"] and out["closed_loop"]["sorties"]
    assert s_closed.st["litres"] <= s_open.st["litres"] * 1.02

def test_valve_never_exceeds_the_plan_per_cell(session_with_herbicide_op):
    s, op = session_with_herbicide_op
    res = fly_closed_loop(s, op, valve=True)
    planned = res.prediction.dose_l_ha
    assert np.all(res.dose_l_ha <= planned * 1.0001 + 1e-9)

def test_stops_when_nothing_left(session_with_herbicide_op, monkeypatch):
    s, op = session_with_herbicide_op
    monkeypatch.setattr(s.twin, "herbicide_targets", lambda w: ("none", np.zeros(len(w), bool)))
    res = fly_closed_loop(s, op)
    assert res.stopped == "done" and len(res.sorties) == 1

def test_every_correction_is_audited(session_with_herbicide_op):
    s, op = session_with_herbicide_op
    res = fly_closed_loop(s, op)
    assert all({"sortie", "channel", "correction", "reason"} <= set(a) for a in res.audit)

def test_restore_replays_the_same_closed_loop(session_with_herbicide_op):
    s, op = session_with_herbicide_op
    s.fly_closed_loop(op.id)
    r = SeasonSession.restore(s_kwargs(s), s.actions)
    assert r.st["litres"] == pytest.approx(s.st["litres"]) and np.allclose(r.dose_cum["herbicide"], s.dose_cum["herbicide"])

def test_live_field_refuses_simulated_closed_loop(live_session_with_op):
    s, op = live_session_with_op
    with pytest.raises(ValueError, match="live field"):
        s.fly_closed_loop(op.id)
```

- [ ] **Step 2: Run** `pytest tests/test_closedloop_run.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `run.py` and the session action.
- [ ] **Step 4: Run** `pytest tests/test_closedloop_run.py tests/test_session*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(closed-loop): fly a job that corrects itself between sorties`

---

### Task 6: Message schema, live run service and routes

**Files:**
- Create: `packages/fa_core/fa_core/live_msgs.py`, `services/console/fa_console/live_run.py`, `services/console/fa_console/routes/live_run.py`
- Modify: `services/console/fa_console/app.py` (register routes and the `live_run` job kind), `services/console/fa_console/sessions.py` (`fly_closed_loop` passthrough to `act`), `services/console/fa_console/exports.py` (`targets_kml`)
- Test: `tests/test_live_msgs.py`, `tests/test_console_live_run.py`

**Interfaces:**
- Produces:
  - **`fa_core.live_msgs` (schema v1).** `SCHEMA = "fa.live/1"`. A batch is a dict:
    - `schema`, `run_id: str`, `seq: int ≥ 0`, `sent_at: ISO-8601`
    - `telemetry`: list of items `{t, lat, lon, alt_agl_m, heading_deg, speed_ms, spraying: bool, sortie: int, flow_lpm?, tank_l?, soc?}`
    - `cells`: list of items `{t, cell: int, weeds: int ≥ 0, area_m2: float > 0, gsd_m: float, blight: bool, cc?: float 0–1}`
  - `validate_batch(batch: dict, n_cells: int, polygon_wgs84=None) -> tuple[dict, list[str]]` returns the batch with only valid items and a list of per-item errors (`"telemetry 3: lat is not a number"`). It raises `ValueError` only for a bad envelope (schema, run_id, seq).
  - **`LiveRunService(store, farms, sessions, jobs, devices, exports)`**, with these methods:
    - `start_virtual(fid, op_id, valve: bool) -> dict` submits a job `live_run`. The job calls `sessions.act(fid, "fly_closed_loop", …)` with `on_sortie` publishing to `state`.
    - `start_live(fid, op_id, token_device_id) -> dict` freezes the prediction and creates `run_id`.
    - `ingest(token, batch) -> {"accepted", "duplicate": bool, "errors"}` is idempotent on `(run_id, seq)`, and the seq set is persisted in the farm dir.
    - `state(fid) -> dict` returns prediction summary, sorties, current drone position, footprint polygon (WGS84), detections (cell, est), deviation cells per channel, remaining targets, audit.
    - `freeze(fid) -> dict` switches to open loop: re-plans stop and an audit entry is written.
    - `next_sortie_kml(fid) -> bytes` uses `exports.targets_kml`.
    - `finish_live(fid) -> dict` turns the accumulated cells into a partial `ScanObservation` (Task 1 mask) and calls `sessions.upload_scan`-equivalent `scan_now(obs=…)`.
  - **Routes:**
    - `POST /api/farms/{fid}/live-run` body `{op_id, valve?}`
    - `GET /api/farms/{fid}/live-run`
    - `POST /api/farms/{fid}/live-run/freeze`
    - `POST /api/farms/{fid}/live-run/finish`
    - `GET /api/farms/{fid}/live-run/next-sortie.kml`
    - `POST /api/live-run/ingest` with header `X-Device-Token`; this route is not subject to the origin guard because a device posts it, as for `/api/ingest`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_live_msgs.py
def test_bad_items_rejected_individually():
    b = _batch(telemetry=[_tel(), _tel(lat=float("nan"))], cells=[_cell(5), _cell(10_000), _cell(3, weeds=-1)])
    ok, errs = validate_batch(b, n_cells=100)
    assert len(ok["telemetry"]) == 1 and [c["cell"] for c in ok["cells"]] == [5]
    assert len(errs) == 3 and any("lat" in e for e in errs)

def test_bad_envelope_raises():
    with pytest.raises(ValueError, match="schema"):
        validate_batch({**_batch(), "schema": "x"}, 100)

# tests/test_console_live_run.py
def test_virtual_live_run_publishes_each_sortie(client, farm_with_herbicide_op):
    fid, op = farm_with_herbicide_op
    j = client.post(f"/api/farms/{fid}/live-run", json={"op_id": op}).json()
    wait_job(client, j["id"])
    st = client.get(f"/api/farms/{fid}/live-run").json()
    assert st["sorties"] and st["prediction"]["litres"] > 0 and "remaining" in st

def test_replayed_and_out_of_order_batches_count_once(client, live_run):
    fid, tok, run = live_run
    b1, b2 = _batch(run, seq=1, cells=[_cell(1)]), _batch(run, seq=2, cells=[_cell(2)])
    for b in (b2, b1, b2, b1):
        client.post("/api/live-run/ingest", json=b, headers={"X-Device-Token": tok})
    st = client.get(f"/api/farms/{fid}/live-run").json()
    assert st["cells_seen"] == 2 and st["batches"] == 2

def test_ingest_needs_a_device_token(client, live_run):
    fid, _, run = live_run
    assert client.post("/api/live-run/ingest", json=_batch(run, 1)).status_code == 401

def test_freeze_stops_corrections_and_is_audited(client, live_run):
    fid, tok, run = live_run
    client.post(f"/api/farms/{fid}/live-run/freeze")
    st = client.get(f"/api/farms/{fid}/live-run").json()
    assert st["mode"] == "open loop" and st["audit"][-1]["correction"] == "plan frozen by the operator"

def test_next_sortie_kml_covers_remaining_targets(client, live_run):
    fid, _, _ = live_run
    r = client.get(f"/api/farms/{fid}/live-run/next-sortie.kml")
    assert r.status_code == 200 and b"<Polygon>" in r.content
```

- [ ] **Step 2: Run** `pytest tests/test_live_msgs.py tests/test_console_live_run.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the schema, service and routes. Persist live-run state as `farm_dir/live_run/{run_id}.json`, plus the seq set, so a console restart keeps it.
- [ ] **Step 4: Run** `pytest tests/test_live_msgs.py tests/test_console_live_run.py tests/test_console_*.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(console): live run — ingest, re-plan, next-sortie file`

---

### Task 7: Live view

**Files:**
- Create: `apps/console/src/pages/LiveRun.tsx`
- Modify:
  - `apps/console/src/pages/Mission.tsx`: for each open spray op, buttons **Fly closed loop** (virtual) / **Start live run** (live field).
  - `apps/console/src/three/FarmScene.ts`: `showFootprint(poly | null)`, `showDetections(cells: {i: number; v: number}[])`, `setHighlight(cells: number[], color)`, layer `'deviation'` added to `Layer`.
  - `apps/console/src/api.ts`: `LiveRunState` type.
  - `apps/console/src/router.ts`: page `'live-run'`.
- Test: `tests/e2e/test_ui.py::test_closed_loop_live_view`

**Interfaces:**
- Consumes: `GET /api/farms/{fid}/live-run` (Task 6 `state`), polled every 1 s while running.
- Produces the page `#/f/{fid}/live-run`, which shows:
  - the 3D farm with the drone at its latest position, the camera footprint, detections, the deviation layer and the remaining targets;
  - a sortie log table (sortie, cells seen, cells sprayed, litres, correction and why);
  - planned vs flown totals (litres, sorties, minutes);
  - **Freeze plan (open loop)** and **Download next sortie (KML)** buttons.

- [ ] **Step 1: Write the failing test**

```python
def test_closed_loop_live_view(page, console):
    fid, op = farm_with_herbicide_op_via_api(console)
    page.goto(console["base"] + f"/#/f/{fid}/mission")
    page.get_by_role("button", name="Fly closed loop").first.click()
    page.wait_for_url("**/live-run", timeout=20_000)
    page.get_by_role("heading", name="Live run").wait_for()
    page.get_by_role("table", name="Sortie log").get_by_role("row").nth(1).wait_for(timeout=60_000)
    for t in ("Planned", "Flown", "Freeze plan (open loop)", "Download next sortie (KML)"):
        assert page.get_by_text(t, exact=False).first.is_visible()
    assert _canvas_variance(page) > 5
    shot(page, "14_live_run")
    assert not page.errors, page.errors
```

- [ ] **Step 2: Run** `cd apps/console && npm run build && cd ../.. && FA_E2E=1 pytest tests/e2e/test_ui.py -k live_view -v`. Expected: FAIL (no button).
- [ ] **Step 3: Implement** the page and the scene methods. Check the screenshot in both themes.
- [ ] **Step 4: Run** `FA_E2E=1 pytest tests/e2e -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat(console): live run view` (include the rebuilt `static/`)

---

### Task 8: Benchmark and ADR 0018

**Files:**
- Create: `ml/closed_loop_bench.py`, `ml/h200/closed_loop_bench.sh`, `docs/adr/0018-live-closed-loop.md`
- Modify: `docs/console.md` (a "Live run" section), `README.md` (one line)
- Test: `tests/test_closed_loop_bench.py`

**Interfaces:**
- Consumes: `SeasonSession`, `fly_closed_loop`, the template archetypes (`load_archetypes`).
- Produces: `run_bench(templates: list[str], seeds: list[int], out: Path, workers: int) -> dict`.
  - For each template × seed, run the session to the first herbicide op, then fork three ways by `restore`: open (`fly_now`), closed, and closed + valve.
  - Finish each fork's season.
  - Record per arm: herbicide litres, truth weeds m⁻² 7 days after the flight, yield t/ha, net per ha, sorties, flight minutes.
  - Write `out/closed_loop.json` (per-run rows plus per-arm mean, sd and paired difference vs open with a 95 % CI) and `out/closed_loop.md`.
  - CLI: `python ml/closed_loop_bench.py --templates all --seeds 0-9 --workers N`.

- [ ] **Step 1: Write the failing test**

```python
def test_bench_runs_and_reports_paired_differences(tmp_path):
    r = run_bench(["<smallest template id>"], [0, 1], tmp_path, workers=1)
    arms = r["summary"]
    assert set(arms) == {"open", "closed", "closed_valve"}
    for a in ("closed", "closed_valve"):
        assert {"litres_diff_mean", "litres_diff_ci95", "net_diff_mean", "weeds_after_diff_mean"} <= set(arms[a])
    assert (tmp_path / "closed_loop.md").exists()
```

(Mark the test `slow` if it exceeds 60 s.)

- [ ] **Step 2: Run** `pytest tests/test_closed_loop_bench.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `closed_loop_bench.py` with a `ProcessPoolExecutor` over runs, and the H200 wrapper script (same conventions as `ml/h200/gsd_sweep.sh`).
- [ ] **Step 4: Run** the test (PASS), then a local benchmark: `python ml/closed_loop_bench.py --templates all --seeds 0-4 --workers 4`.
- [ ] **Step 5: Write the ADR from the measured numbers only.** ADR 0018 covers context, decision, the measured result table with spread, the limits (simulated camera, per-sortie split approximation, fungicide field-level), and "what to measure on real hardware". Write the console.md section too.
- [ ] **Step 6: Commit** `docs(adr): 0018 live closed loop with measured benchmark`

After commit: run the full benchmark on the H200 (`seeds 0-19`), replace the ADR table with those numbers, and commit again.

---

## Self-review notes

- **Spec coverage:**
  - Three loops: in-pass is the valve (Task 5); between sorties is Tasks 4–5; after the job is `_account` plus `finish_live`, then the season re-plan (Tasks 5–6).
  - Deviation channels:
    - crop: weeds (Task 2), canopy (field-level, Task 2);
    - dose (Task 2);
    - track: `kpis["skipped_frac"]` per sortie feeds the dose channel's skipped-strip rule. The spec's GNSS cross-track error needs real telemetry, so it moves to plan 2.
  - Live view (Task 7); safety rules (Global Constraints, Task 5 valve test, Task 6 freeze); benchmark and ADR (Task 8).
  - **Not covered here:** on-board agent and actuation adapters (plan 2); MAVLink mission output (plan 2, adapter); blight per cell (the twin has no per-cell blight; fungicide re-plan is field-level — stated in the ADR limits).
- **Types:** `Prediction`, `Deviation`, `SortieReport` and `ClosedLoopResult` are defined in Tasks 2 and 5 and consumed by name in Tasks 6–8. `herbicide_targets` (Task 1) is used in Tasks 4–5. The schema item shape is defined in Task 6 and produced by `to_cell_messages` (Task 3).
