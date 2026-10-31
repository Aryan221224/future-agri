# 0009 — Permissive licences only, enforced in CI

**Status:** accepted · **Date:** 2026-09-09

## Policy

No AGPL or GPL/LGPL package may enter the runtime dependency tree.
`scripts/check_licences.py` fails the build if one does.

## Why this needs enforcement rather than good intentions

The real risk is **transitive and non-obvious**:

* **CODS** — the only correct conformal-object-detection library, MIT-licensed —
  hard-depends on `ultralytics` (AGPL-3.0) purely for YOLO wrappers we would
  never call. Installing it drags AGPL in sideways.
* **TorchCP** is the most-cited conformal library and is LGPL-3.0/GPL-3.0.
* **YOLO-NAS** ships Apache-2.0 *code* with proprietary non-commercial
  *weights* — worse than AGPL, which at least offers a compliance path.
* **DEIMv2**'s licence forbids commercial use outright, while at least one
  widely-read model-licensing page reports it as Apache-2.0.
* **D-FINE** is Apache-2.0, but its Objects365-pretrained checkpoints inherit
  that dataset's terms. Use the COCO-only weights.

**Weights are licensed separately from code.** A repository badge is not a
grant for the checkpoint.

## Isolated exceptions

**OpenDroneMap is AGPL-3.0** and is genuinely the best photogrammetry option
(including multispectral radiometric calibration). It runs as a **separate
container behind NodeODM's HTTP API**, never linked into our process, and we
talk to it through `pyodm` (BSD). That process boundary is the standard,
defensible pattern — but if orthomosaic generation is ever offered to customers
as a network service, AGPL's network clause needs legal review *before* the
architecture depends on it.

**PCSE is EUPL-1.2** (see ADR 0005) — weak copyleft with a network trigger.
Accepted deliberately because there is no permissively-licensed crop model of
comparable quality, and flagged as a watch-item.

## Dataset policy

Ship-model training data must be CC BY / CC0. Specifically usable:
**DRONEWEED** (67.5k UAV bbox images, CC BY 4.0), **WeedsGalore** (5-band UAV
multispectral with instance masks, CC BY 4.0), **msuav500k** (598k unlabelled
UAV frames for SSL pretraining, CC BY 4.0), **DeepWeeds** (CC BY 4.0).

Excluded from anything that ships: **Agriculture-Vision** (non-commercial, no
redistribution, deletion required within 90 days — and it is the dataset
everyone reaches for first in aerial agriculture), **CropAndWeed**, **CWFID**,
**OPPD** (all non-commercial).

CC BY-**SA** data (PhenoBench, Sugar Beets 2016, EuroCrops, PlantVillage) stays
in the research lane: whether trained weights are a derivative work of training
data under SA is legally untested, and the conservative posture is cheap.

Two datasets need written clarification before use: **WeedMap** (no licence
stated on the dataset page) and **Global Wheat Head Detection** ("free to use
for any purpose", no SPDX identifier).
