# Future Agri

A crop digital twin that tells a farmer, every week, whether to **irrigate,
spray fungicide, spray herbicide, or wait** — and how sure it is. A WOFOST crop
model kept in step with the field by one camera pass and ensemble data
assimilation, a forecast-ensemble planner scored in ₹/ha, a calibrated
crop/weed detector whose uncertain calls go to a person, and drone missions
routed within battery and tank limits. Runs end to end with no hardware.

```bash
pip install -r requirements.txt
make demo          # the whole loop, ~45 s, no drone, no GPU, no network
make test          # 122 tests
make bench         # reproduces every number quoted below
make twin-ui       # then open http://localhost:8099/twin.html
```

---

## Why this exists in this shape

This project had a written architecture before it had code. When each of that
architecture's choices was checked against primary sources — SDK support
matrices, licence files, PyPI release dates, real benchmarks — several central
ones turned out to be wrong. Not vague: **wrong**. They named a library that has
since been archived, an algorithm that does not fit the problem, and a
performance gap that does not exist.

The corrections are recorded in [`docs/adr/`](docs/adr/), including the ones
that are unflattering to the earlier design:

| what changed | why |
|---|---|
| MPPI → greedy/beam search | The decision is ~20 sparse *discrete* choices per season, not continuous control. MPPI's softmax-mean is undefined over "urea vs nothing". [ADR 0003](docs/adr/0003-scheduling-search.md) |
| neural crop surrogate → none | PCSE runs a season in **153 ms measured**. The ~850× gap that justifies emulation is an APSIM problem, not ours. [ADR 0004](docs/adr/0004-no-neural-surrogate.md) |
| MinIO → filesystem/Garage | MinIO's repository was **archived by its owner on 2026-04-25**; binaries discontinued, no security-patch commitment. [ADR 0008](docs/adr/0008-data-platform.md) |
| eoAPI bundle → hand-assembled | eoAPI's own compose pins its components up to two majors behind. [ADR 0008](docs/adr/0008-data-platform.md) |
| EMQX → Mosquitto | EMQX went **BSL 1.1 at 5.9**; the last Apache-2.0 release hit EOL 2026-02-28. [ADR 0008](docs/adr/0008-data-platform.md) |
| MC dropout → split conformal | ~30× cheaper inference *and* a distribution-free guarantee. [ADR 0002](docs/adr/0002-conformal-prediction.md) |

---

## Status after the 30 Oct 2026 audit and 31 Oct rebuild

The September numbers in this README (−57.9 % LAI error, −51.3 % yield error,
"one visit is enough") came from a weather generator with **102 mm of rain a
year** and an OSSE with **16 weekly observations**. A section-by-section audit
reran everything on realistic Bennett weather; the rebuild ported what held up
and fixed two filter bugs found afterwards. [ADR 0013](docs/adr/0013-post-audit-rebuild.md)
has the full record. What the system measures now, all in simulation:

### A. The closed loop, season by season

40 paired seasons (same weather, blight arrival and weed field under every
policy; 28 of 40 seasons get blight):

| policy | water (cm) | fungicide sprays | scouting flights | yield (t/ha, farm) | net vs farmer, ₹/ha (95 % CI) |
|---|---:|---:|---:|---:|---|
| farmer: 4 cm every 10 d, spray every 10 d | 40.0 | 8.0 | 0 | 23.65 | — |
| **twin** (risk-neutral, default) | **23.3** | 7.65 | 5.3 | 23.65 | **+3,967** (3,337 – 4,621), better in 40/40 |
| twin, CVaR-80 | 23.3 | 8.57 | 4.9 | 23.70 | +3,208 (2,656 – 3,790), 38/40 |
| **twin + regional inoculum alert** | **23.3** | **5.80** | 0.8 | 23.69 | **+8,461** (6,761 – 10,262), 40/40 |
| no sprays (value of protection) | 40.0 | 0 | 0 | 14.61 | −76,376 |

- **Water: −42 % (16.7 cm) in 40/40 seasons at the farmer's yield.** In the 12
  blight-free seasons yield was 23.92 vs 23.94 t/ha. This is the twin's main value.
- **Fungicide: without an inoculum signal the twin saves almost nothing** (−0.35
  sprays, −4 %) — it matches the calendar's protection instead. The audit's
  −26 % sprays came from a planner that ignored the epidemic it had already
  seen; with blight state propagated it does not hold.
- **With a regional inoculum alert: −2.2 sprays (−28 %) and +₹8,461/ha.** The
  alert is what lets the twin skip spraying in seasons the pathogen never reaches.
- Scouting still costs ~5 flights a season without the alert (~₹1,600/ha); the
  alert cuts it to 0.8.

`bench/policy_comparison.py` — potato at Bennett, 25 Oct – 10 Feb; the twin's
soil parameters wrong by up to ±10 %; economics are stated assumptions (potato
₹10/kg, spray ₹1,500, scouting ₹300/flight, herbicide ₹2,000, water ₹300/cm).

### B. One camera pass: canopy yes, yield no — and the day matters

```
60 trials · 24 members · cover (sd 0.04) + growth stage (sd 0.05) · fixed EnKF,
members re-run from sowing with analysed parameters · 15-day forecast + climatology
pass day 30   30-day canopy RMSE  -18 %   better in 33/60   yield: no gain
pass day 45   30-day canopy RMSE  -51 %   better in 43/60   yield: no gain
80 % yield interval coverage: order-statistic 0.77-0.85 · naive percentile 0.68-0.77
```

`bench/one_pass_osse.py`. Schedule the pass near day 45 for the canopy
forecast; make no yield claim from it.

The filter itself is not the limit. Given the *true* weather and 16 weekly
observations, the same filter cuts yield error by 82 % and canopy error by 75 %,
better in 16/16 trials (`bench/osse_assimilation.py`). With one pass and an
unknown future, the weather after the pass dominates the yield.

### C. Earlier findings, rerun on realistic weather

#### Search width still buys nothing; two September findings reversed

N scheduling on the real crop model (the older paddy N planner), 5 weather
scenarios, CVaR-80, now on normals-calibrated weather:

| method | CVaR (₹/ha) | rollouts | plan |
|---|---:|---:|---|
| do nothing | 4 744 | 5 | — |
| uniform split (naive heuristic) | 10 419 | 5 | 4 × 30 kg N |
| **greedy** | **15 969** | **65** | 1 × 30 kg N, Jul 5 |
| beam-5 | 15 969 | 230 | same |
| MPPI | 14 119 | 205 | 2 × 30 kg N |

- **Greedy matched beam-5 with 3.5× fewer rollouts** — unchanged. Ship greedy.
- **MPPI now loses by 13 %** (on 102 mm/yr weather it had tied).
- **The naive split is now better than doing nothing** (on the old weather it
  was worse). With realistic rain the crop responds to N in most scenarios, so
  the "worse than nothing" argument for CVaR does not hold here.
- This planner advises paddy nitrogen; the shipped potato 8.1 set cannot be
  N-limited at all (ADR 0013), so it is not offered for potato.

`bench/planner_comparison.py`

#### Conformal coverage holds — and the monitor catches it when it stops

On a **synthetic 6-class classifier** (not the detector — real-detector numbers
are under *Honest limitations* and in ADR 0014):

| score | mode | α | coverage | mean set | review rate |
|---|---|---:|---:|---:|---:|
| **LAC** | deterministic | 0.10 | **0.8979** | **2.39** | **77.4 %** |
| RAPS | deterministic | 0.10 | 0.9183 | 2.87 | 94.7 % |
| RAPS | randomized | 0.10 | 0.8952 | 2.57 | 91.1 % |

Deterministic RAPS **over-covers**, and over-coverage is not free: it inflates
set size and therefore human review load, which is the cost that decides whether
anyone keeps using the system. LAC is the production default — near-exact
deterministically, smallest sets, and reproducible, which matters when you must
answer "why was this flagged?" months later.

Exchangeability is the assumption agriculture breaks hardest, so it is tested
rather than assumed:

```
in-distribution   coverage 0.9000   monitor quiet
mild shift        coverage 0.8011   monitor FIRES
severe shift      coverage 0.6522   monitor FIRES
```

`bench/conformal_coverage.py`

#### The review queue, not the imagery, is the real uplink constraint

A 25 ha sortie at 3 cm GSD, measured with real JPEG compression:

```
raw imagery              9 524.8 MB
JPEG imagery             2 048.4 MB
derived products only        5.1 kB     402,189x  less than JPEG
+ review thumbnails         16.3 MB         125x  less than JPEG
```

(The review share here is the synthetic classifier's 77.4 %; the real detector's
marginal gate reviewed 59.7 %, and the crop/weed action gate is built to cut it.)

Edge inference alone is a ~10⁵× reduction. But once uncertain findings must
reach a human, **the review queue dominates the uplink by three orders of
magnitude over the derived products**. The thing to optimise is therefore the
review *rate* — i.e. calibration quality — not the payload format. That was not
obvious before measuring it.

`bench/bandwidth.py`

#### Endurance, not coverage, is what makes a mission hard

Fields2Cover plans routes for a vehicle with no battery — its `Robot` type has
no capacity, energy or refill field anywhere. An agricultural multirotor flies
15–40 min, so *every* real mission is multi-sortie. That decomposition is ours:
OR-Tools with an **arc-based** energy dimension (drain depends on the transition,
not on visiting a node) and one virtual vehicle per sortie.

```
 6.0 ha,  7 swaths ->  1 sortie   6.9 min   88 Wh  (max  88/220 Wh)
24.0 ha, 15 swaths ->  2 sorties 30.9 min  339 Wh  (max 208/220 Wh)
60.0 ha, 22 swaths ->  5 sorties 82.3 min  846 Wh  (max 211/220 Wh)
```

Every plan is re-verified independently of the solver — never trust a solver's
own accounting for a safety constraint. Those are **survey** sorties. A spray
drone is limited by its tank as well: a 6 ha broadcast job with a 10 L tank at
20 L/ha needs at least 12 sorties (liquid is a per-sortie capacity in the router
and in the verifier); spot-spraying skips clean swaths.

---

## The bug that shaped the whole design

Configuring the twin for winter wheat at Ludhiana (30.9° N) — an entirely
reasonable thing for a user to do, in one of the world's largest wheat regions —
produced:

```
TWSO = 0.0 kg/ha     LAImax = 1.43     DVS = 0.066     (no exception, no warning)
```

Every wheat cultivar in the WOFOST parameter repository is European and
photoperiod-sensitive with an optimum daylength of 16.3–16.8 h. The longest day
at 30.9° N is **14.72 h**. Development stalls, no grain fills, and the model
returns a confident zero after simulating a full season of biomass growth.

A crash is honest. **A zero that looks like a number is not** — it flows into a
dashboard, an average, a recommendation. So the twin now runs feasibility gates
and refuses rather than returning a suspect number ([ADR 0011](docs/adr/0011-feasibility-gates.md)):

```
$ make demo
  wheat/Winter_wheat_101   feasible=False
     ! cultivar is photoperiod-sensitive (IDSL=2) with DLO=16.8 h, but the longest
       day at latitude 30.90 is 14.7 h. Development will stall and yield will be
       reported as 0 with no error.
  rice /Rice_501           feasible=True
```

The same principle now runs throughout: the router re-verifies energy
feasibility, the conformal layer refuses under-sampled strata and says so, the
EnKF rejects observations >5σ from the ensemble (a cloud, not a crop), synthetic
weather stamps `NOT-FOR-ADVICE` into every summary it touches, and crops whose
parameters are 1988 generic placeholders (cotton, sugarcane) carry that warning
all the way to the UI.

---

## Architecture

```
                         ┌──────────── cloud (intermittent) ────────────┐
  aircraft               │                                              │
  ┌──────────┐   frames  │   twin (WOFOST + EnKF)   planner (greedy/    │
  │ certified│──────────▶│          │                    CVaR + OR-Tools)│
  │  OEM     │           │          ▼                         │         │
  └──────────┘           │   observation store ◀── ingest ◀───┼── MQTT  │
        ▲                │   (PostGIS + TimescaleDB)          │  bridge │
        │ mission        └────────────────────────────────────┼─────────┘
        │                                                     │ store-and-forward
  ┌─────┴───────────────── field-edge gateway ────────────────┴──────────┐
  │  perception (TensorRT)  →  conformal gate  →  derived products       │
  │                              │                                       │
  │                              └─▶ review queue (imagery stays here)   │
  └──────────────────────────────────────────────────────────────────────┘
```

Three loops at three timescales, because a single "closed loop" cannot be both
offline-capable and cloud-controlled:

| tier | closes at | timescale | needs connectivity |
|---|---|---|---|
| reflex | the aircraft's certified flight stack | 1–100 ms | none |
| tactical | the field-edge gateway | 1 s – 5 min | local radio only |
| strategic | cloud | hours – days | intermittent; store-and-forward |

### Layout

```
packages/fa_core/      geoid · imaging geometry · domain model · conformal prediction
                       (marginal, class-conditional, action-level gates)
services/twin/         WOFOST engine · normals-calibrated weather + forecast ensemble ·
                       ensemble Kalman assimilation (LAI, canopy cover, growth stage) ·
                       blight + weed damage layer
services/planner/      coverage · energy- and tank-constrained routing · weekly planner
                       (fungicide / herbicide / scouting) · irrigation scheduler ·
                       SOP spray windows · N scheduling (greedy/beam/MPPI)
services/loop/         FieldTwin orchestrator · one-pass ingest · season simulator
services/gateway/      edge perception · conformal gate · zone aggregation
services/api/          FastAPI: observations, review queue, coverage monitor,
                       /twin advisory endpoints (no database needed)
services/ingest/       MQTT → TimescaleDB, idempotent
sim/                   kinematic simulator (always works) · PX4/MAVSDK (real)
db/migrations/         PostGIS + TimescaleDB schema
bench/                 every number in this README
ml/                    detector training, conformal calibration on real images, ONNX/TensorRT export
docs/adr/              14 decision records, including the reversals
web/                   field dashboard (index.html) · twin advice + review labelling (twin.html)
```

---

## Data model: adopted, not invented

- **farmOS** Asset / Log / Quantity, reimplemented over PostGIS. Its Asset/Log
  split, the "happened **to**" vs "happened **in**" distinction, and
  pending/done/abandoned status (which gives planned-vs-executed for free) are
  the load-bearing ideas.
- **OpenAgri OCSM** land hierarchy: Farm → Site → Parcel → ManagementZone.
- **ISO 11783-10 (ISOXML)** for prescriptions and as-applied data — day one, not
  roadmap. If a prescription cannot load into the controller a grower already
  owns, there is no product.
- **Content-addressed field identity** (AgStack's S2-cover idea, implemented
  directly): stable under vertex reordering, ring winding and sub-metre jitter,
  so one field digitised twice by two people resolves to one record.

Eclipse Ditto — the obvious "digital twin state store" — was rejected for a
concrete reason: **its query language has no geospatial operators at all**, and
our primary query is "which twins are inside this boundary."

---

## Licensing is enforced, not documented

`make licences` fails the build if AGPL/GPL enters the runtime tree, because the
real risk is transitive: CODS is MIT but hard-depends on `ultralytics` (AGPL);
TorchCP is the most-cited conformal library and is LGPL/GPL. Weights are
licensed separately from code — YOLO-NAS ships Apache-2.0 code with proprietary
non-commercial weights, which is worse than AGPL because AGPL at least offers a
compliance path.

Documented exceptions: **PCSE (EUPL-1.2)** — no permissive crop model of
comparable quality exists; **OpenDroneMap (AGPL)** — isolated behind NodeODM's
HTTP boundary, reached via `pyodm` (BSD).

Training data for any shipped model is CC BY / CC0 only. Notably excluded:
**Agriculture-Vision**, the dataset everyone reaches for first in aerial
agriculture, which forbids commercial use and requires deletion within 90 days.

---

## Honest limitations

- **No shipped crop parameters are calibrated for India.** Wheat cultivars are
  European and cannot complete development at Indian latitudes at all; rice
  offers IR8 and IRRI hybrids and no Indian releases; **cotton and sugarcane have
  exactly one generic set each, Van Heemst 1988, "no specific region"**. Budget
  1–2 seasons of field data per crop × zone before absolute yield is
  decision-grade. Lead with relative signals ("15 % behind the district norm"),
  which are defensible far sooner.
- **WOFOST has no puddled-transplanted-rice water balance**, and Indian kharif
  rice is grown in exactly that system. Not calibratable away.
- **Perception is measured on German maize, not on an Indian field.**
  RF-DETR-Medium on WeedsGalore (H200, 30 Oct 2026): test mAP50:95 0.281, AP50
  0.487; maize 0.91 AP50, quickweed 0.25, other weeds 0.11. It has no potato,
  wheat or disease classes. On its real scores the marginal gate hides a weed it
  covers only 37 % of the time; the class-conditional gate lifts that to 81 % (at
  70.7 % review), and the crop/weed action gate cuts review to 38.5 % but
  under-covers the weed group on later dates (0.82 vs 0.90). The gate must be
  recalibrated on 100–200 field labels, by date, before its coverage means
  anything here ([ADR 0014](docs/adr/0014-action-level-class-conditional-gate.md)).
  The ONNX export runs, but **parity with PyTorch is unproven**: only 11/20
  images (and 7/20 for PyTorch fp16) gave identical decisions under a strict
  per-query check that query reordering in RF-DETR's two-stage selection can
  break. `ml/export.py` now also reports matched-detection agreement; rerun it
  on the H200 before deploying the ONNX model. Latency on an edge device has not
  been measured (none chosen; the 122 ms figure is ONNX on the server's CPU).
- **Blight, weed and price figures are assumptions.** The fungicide, scouting and
  herbicide verdicts move with them; the blight model infects ~65 % of seasons.
- **The kinematic simulator is not a flight-dynamics simulator.** It models
  scheduling and energy. Anything depending on aerodynamics, wind, control loops
  or GPS error must be tested in PX4 SITL (`make sitl`).
- **The weather is a generator calibrated to IMD normals, not station data.** It
  reproduces Delhi's statistics (755 mm/yr vs 774 normal) but not real days, and
  is flagged NOT-FOR-ADVICE in every summary. Replace with IMD or station data.
- **The irrigation result is optimistic for a field.** The simulated twin knew the
  past weather exactly, WOFOST 7.2 does not penalise over-watering, and the
  baseline is a fixed schedule. The field trial should meter water against a
  cheap soil-moisture sensor as well as the farmer.
- **Nothing here has flown.** Every result is from simulation or an OSSE. The
  field measurements required to validate it are listed in
  [ADR 0005](docs/adr/0005-crop-model.md).

---

## Reproducing

```bash
make test                       # 122 tests, ~2.5 min
make demo                       # end-to-end closed loop, ~45 s
make bench-quick                # small versions of the headline benchmarks, ~5 min
make bench                      # every number in this README, ~60 min on 2 cores
make gate-eval                  # conformal gates on the real detector's cached scores
make licences                   # copyleft gate
make up                         # docker: postgis+timescale, mosquitto, api, ingest, web
make sitl                       # the verified PX4 SITL setup
```

Verified stack for real flight (these versions are not interchangeable — see
[ADR 0007](docs/adr/0007-simulation-stack.md)):

```
Ubuntu 24.04 LTS · PX4-Autopilot v1.17.0 · Gazebo Harmonic (gz-sim8) · mavsdk 3.17.2
```

One thing that will cost you a day if nobody says it: **Gazebo's camera sensor
is not wired to the MAVLink camera protocol.** A mission `CameraAction` fires the
mission item and produces no image. Frames must be pulled off the Gazebo
Transport topic and stamped with an interpolated pose, or photogrammetry has
nothing to georeference.
