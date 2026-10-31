# Future Agri

A closed-loop digital twin for agricultural UAV operations: coverage planning
under battery endurance, edge perception with calibrated uncertainty, a WOFOST
crop twin with ensemble data assimilation, and risk-aware intervention
scheduling — running end to end with no hardware.

```bash
pip install -r requirements.txt
make demo          # the whole loop, ~45 s, no drone, no GPU, no network
make test          # 72 tests
make bench         # reproduces every number quoted below
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

## What the system does, and what it measured

### 1. Assimilating UAV LAI genuinely improves the crop estimate

An OSSE — perturb the crop parameters to make a "truth", sample noisy LAI at a
realistic flight cadence, then run the ensemble open-loop and with the EnKF.
Comparison is **paired** per truth draw, because the ratio-of-medians can report
a loss while a clear majority of trials improve. (It did, on the first run.)

```
16 trials · 30 members · survey every 7 d · LAI retrieval sd 25% of signal

LAI RMSE       open loop 0.130  ->  assimilated 0.052    -57.9% median, better in 14/16
Yield |error|  open loop   363  ->  assimilated   234    -51.3% median, better in 13/16
```

`bench/osse_assimilation.py`

### 2. Search width buys nothing; the naive heuristic is worse than nothing

All methods on the real crop model, N-limited paddy, 5 weather scenarios,
CVaR-80 objective:

| method | CVaR (₹/ha) | rollouts | plan |
|---|---:|---:|---|
| do nothing | 7 265 | 5 | — |
| **uniform split** (naive heuristic) | **3 484** | 5 | 4 × 30 kg N |
| **greedy** | **9 034** | **65** | 1 × 30 kg N, Jul 5 |
| beam-5 | 9 034 | 230 | same |
| MPPI | 9 034 | 155 | same |

Three findings, one of which contradicts what the design predicted:

- **MPPI did not lose** — it tied. The structural objections stand; at this
  problem size they did not bite. Recorded because the tempting move was to
  claim a win.
- **Greedy matched beam-5 with 3.5× fewer rollouts.** Ship greedy.
- **The naive heuristic scored worse than doing nothing** despite a *higher mean
  margin* (17 752 vs 7 780). Four fixed-cost passes paid in every scenario
  against a yield gain that fails in the bad ones. This is the argument for
  optimising CVaR rather than the mean, and it is why CVaR is the default.

`bench/planner_comparison.py`

### 3. Conformal coverage holds — and the monitor catches it when it stops

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

### 4. The review queue, not the imagery, is the real uplink constraint

A 25 ha sortie at 3 cm GSD, measured with real JPEG compression:

```
raw imagery              9 524.8 MB
JPEG imagery             2 048.4 MB
derived products only        4.9 kB     415,909x  less than JPEG
+ review thumbnails         16.3 MB         125x  less than JPEG
```

Edge inference alone is a ~10⁵× reduction. But once uncertain findings must
reach a human, **the review queue dominates the uplink by three orders of
magnitude over the derived products**. The thing to optimise is therefore the
review *rate* — i.e. calibration quality — not the payload format. That was not
obvious before measuring it.

`bench/bandwidth.py`

### 5. Endurance, not coverage, is what makes a mission hard

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
own accounting for a safety constraint.

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
services/twin/         WOFOST engine · weather · ensemble Kalman assimilation
services/planner/      coverage · energy-constrained routing · CVaR scheduling
services/gateway/      edge perception · conformal gate · zone aggregation
services/api/          FastAPI: observations, review queue, coverage monitor
services/ingest/       MQTT → TimescaleDB, idempotent
sim/                   kinematic simulator (always works) · PX4/MAVSDK (real)
db/migrations/         PostGIS + TimescaleDB schema
bench/                 every number in this README
ml/                    detector training, conformal calibration on real images, ONNX/TensorRT export
docs/adr/              12 decision records, including the reversals
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
- **Perception is being trained, not yet measured.** `ml/` trains RF-DETR on
  WeedsGalore (CC BY 4.0, German maize) on an RTX A6000, calibrates the conformal
  gate on real detections and exports ONNX / TensorRT ([ml/README.md](ml/README.md),
  [ADR 0012](docs/adr/0012-detection-conformal.md)). Until `ml/results/` holds a
  run, the gateway still falls back to the deterministic stub, which stamps
  `not for real advice` on everything it touches. The model will have seen no
  Indian field.
- **The kinematic simulator is not a flight-dynamics simulator.** It models
  scheduling and energy. Anything depending on aerodynamics, wind, control loops
  or GPS error must be tested in PX4 SITL (`make sitl`).
- **Synthetic weather is not a climate model** and is flagged as such in every
  summary that uses it.
- **Nothing here has flown.** Every result is from simulation or an OSSE. The
  field measurements required to validate it are listed in
  [ADR 0005](docs/adr/0005-crop-model.md).

---

## Reproducing

```bash
make test                       # 72 tests
make demo                       # end-to-end closed loop, ~45 s
make bench                      # all four benchmarks, ~10 min
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
