#!/usr/bin/env python3
"""End-to-end closed loop, runnable on any machine with no hardware.

    field boundary
      -> stable field identity (geoid)
      -> survey geometry from a target GSD
      -> coverage swaths
      -> energy-feasible sorties (this is where endurance actually binds)
      -> flight (kinematic sim, or PX4 SITL when available)
      -> edge perception with conformal gating
      -> derived products + a human review queue
      -> LAI assimilation into the WOFOST twin
      -> risk-aware intervention schedule
      -> a prescription with an explicit compliance gate

Every number printed is computed, not quoted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/twin", "services/planner",
          "services/gateway", "sim"):
    sys.path.insert(0, str(ROOT / p))

from shapely.geometry import Polygon, mapping  # noqa: E402

from fa_core.geo import CAMERAS, area_ha, plan_survey, to_local, to_wgs84  # noqa: E402
from fa_core.geoid import geoid  # noqa: E402
from fa_core.model import (Asset, AssetType, Log, LogStatus, LogType, Measure,  # noqa: E402
                           Prescription, PrescriptionCell, Quantity)
from fa_core.uncertainty import (StratifiedCalibrator, calibrate_classification,  # noqa: E402
                                 calibrate_regression)
from fa_gateway.perception import StubDetector  # noqa: E402
from fa_gateway.pipeline import EdgePipeline  # noqa: E402
from fa_planner.coverage import order_boustrophedon, plan_coverage  # noqa: E402
from fa_planner.routing import DroneModel, plan_sorties, verify  # noqa: E402
from fa_planner.scheduler import (Economics, ScenarioEvaluator, beam_search,  # noqa: E402
                                  decision_epochs)
from fa_sim.kinematic import KinematicDrone  # noqa: E402
from fa_twin.assimilation import EnsembleTwin, Observation, ParameterPerturbation  # noqa: E402
from fa_twin.engine import ZoneConfig, check_photoperiod  # noqa: E402
from fa_twin.weather import make_synthetic_weather  # noqa: E402

# A real field near Ludhiana, Punjab, as a lon/lat polygon.
LON, LAT = 75.85, 30.90
CLASSES = ["crop", "broadleaf_weed", "grass_weed", "disease_lesion",
           "nutrient_deficiency", "unknown"]


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "-" * len(title))


def build_calibrator(alpha: float, seed: int = 0) -> StratifiedCalibrator:
    rng = np.random.default_rng(seed)
    n = 4000
    y = rng.integers(0, len(CLASSES), n)
    lg = rng.normal(0, 1, (n, len(CLASSES)))
    lg[np.arange(n), y] += rng.uniform(0.8, 3.5, n)
    e = np.exp(lg - lg.max(1, keepdims=True))
    probs = e / e.sum(1, keepdims=True)
    cls = calibrate_classification(probs, y, CLASSES, alpha=alpha, score="lac")
    truth = rng.uniform(0.2, 5.0, n)
    pred = truth + rng.normal(0, 0.25 * truth)
    reg = calibrate_regression(truth, pred, alpha=alpha)
    c = StratifiedCalibrator(alpha=alpha, global_cal=cls, min_n=200)
    c.strata[StratifiedCalibrator.key("rice", "tillering", "ludhiana")] = cls
    c.reg = reg
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--width-m", type=float, default=700.0)
    ap.add_argument("--height-m", type=float, default=420.0)
    ap.add_argument("--gsd-cm", type=float, default=3.0)
    ap.add_argument("--alpha", type=float, default=0.10)
    ap.add_argument("--members", type=int, default=20)
    ap.add_argument("--scenarios", type=int, default=4)
    ap.add_argument("--out", type=Path, default=ROOT / "bench" / "results" / "demo.json")
    a = ap.parse_args()
    t_start = time.perf_counter()
    report: dict = {}

    # ---------------------------------------------------------------- field
    rule("1. Field identity")
    m_per_deg = 111_320.0
    dw = a.width_m / (m_per_deg * np.cos(np.radians(LAT)))
    dh = a.height_m / m_per_deg
    field_ll = Polygon([(LON, LAT), (LON + dw, LAT), (LON + dw, LAT + dh), (LON, LAT + dh)])
    fid = geoid(field_ll)
    ha = area_ha(field_ll)
    local, crs = to_local(field_ll)
    print(f"  geoid       {fid}")
    print(f"  area        {ha:.2f} ha        projected CRS {crs.to_epsg()}")
    field = Asset(type=AssetType.land, name="Ludhiana Plot 12", geoid=fid,
                  geometry=mapping(field_ll))
    report["field"] = {"geoid": fid, "area_ha": round(ha, 2), "epsg": crs.to_epsg()}

    # ------------------------------------------------------------- feasibility
    rule("2. Can the twin even model this crop here?")
    cfg = ZoneConfig(zone_id=fid, latitude=LAT, longitude=LON, crop="rice",
                     variety="Rice_501", sowing_date=dt.date(2025, 6, 20),
                     harvest_date=dt.date(2025, 10, 25), WAV=35.0,
                     NAVAILI=5.0, NSOILBASE=2.0)
    for crop, variety in (("wheat", "Winter_wheat_101"), ("rice", "Rice_501")):
        probe = ZoneConfig(**{**cfg.__dict__, "crop": crop, "variety": variety})
        f = check_photoperiod(probe)
        print(f"  {crop:5s}/{variety:18s} feasible={f.ok}")
        for r in f.reasons:
            print(f"        ! {r}")
    report["feasibility_rejected_wheat"] = not check_photoperiod(
        ZoneConfig(**{**cfg.__dict__, "crop": "wheat", "variety": "Winter_wheat_101"})).ok

    # ------------------------------------------------------------- mission
    rule("3. Survey geometry and coverage")
    cam = CAMERAS["mavic3m_ms"]
    sp = plan_survey(cam, a.gsd_cm / 100.0)
    cov = plan_coverage(local, sp.line_spacing_m)
    ordered = order_boustrophedon(cov.swaths)
    print(f"  camera      {cam.name}")
    print(f"  altitude    {sp.altitude_m:.0f} m for {a.gsd_cm:.0f} cm GSD "
          f"(swath {sp.swath_m:.0f} m, spacing {sp.line_spacing_m:.1f} m)")
    print(f"  coverage    {len(cov.swaths)} swaths at {cov.angle_deg:.0f} deg, "
          f"{cov.total_swath_length_m:.0f} m, ratio {cov.coverage_ratio:.2f} [{cov.backend}]")

    rule("4. Energy-constrained sortie decomposition")
    drone = DroneModel()
    origin = local.exterior.coords[0]
    mission = plan_sorties(ordered, origin, drone, time_limit_s=5)
    problems = verify(mission)
    s = mission.summary()
    print(f"  {s['sorties']} sorties, {s['total_time_min']:.0f} min flying, "
          f"{s['total_energy_wh']:.0f} Wh total")
    print(f"  worst sortie {s['max_sortie_energy_wh']:.0f} Wh against a "
          f"{s['energy_budget_wh']:.0f} Wh budget")
    print(f"  independent verification: {'PASS' if not problems else problems}")
    report["mission"] = s | {"verified": not problems, "swaths": len(cov.swaths)}

    # ------------------------------------------------------------- flight
    rule("5. Flight (kinematic simulator)")
    sim = KinematicDrone(drone, home=origin, altitude_m=sp.altitude_m,
                         trigger_distance_m=sp.trigger_distance_m)
    flights = sim.fly_mission(mission)
    frames_total = sum(f.n_frames for f in flights)
    for i, f in enumerate(flights):
        print(f"  sortie {i}: {f.duration_s/60:5.1f} min, {f.energy_wh:6.1f} Wh, "
              f"{f.n_frames:4d} frames, completed={f.completed}"
              + (f" ({f.aborted_reason})" if not f.completed else ""))
    print(f"  total {frames_total} frames captured")
    report["flight"] = {"frames": frames_total,
                        "all_completed": all(f.completed for f in flights),
                        "total_min": round(sum(f.duration_s for f in flights) / 60, 1)}

    # ------------------------------------------------------------- perception
    rule("6. Edge perception with conformal gating")
    calib = build_calibrator(a.alpha)
    det = StubDetector(classes=CLASSES)
    pipe = EdgePipeline(det, calib, CLASSES, crop="rice", stage="tillering", site="ludhiana")
    rng = np.random.default_rng(1)
    sample = [(rng.random((256, 256, 3)) * 255).astype(np.uint8) for _ in range(24)]
    summary, reviews = pipe.process_zone(fid, sample, ha, dt.datetime.now(dt.timezone.utc))
    scale = frames_total / len(sample)
    print(f"  detector    {det.model_version} (synthetic -- not for real advice)")
    print(f"  target coverage {1 - a.alpha:.0%}, mean conformal set "
          f"{summary.mean_conformal_set_size:.2f}")
    print(f"  {summary.n_detections} detections in {len(sample)} sampled frames; "
          f"{summary.review_fraction:.0%} routed to human review")
    print(f"  uplink payload {summary.payload_bytes()} B per zone "
          f"(vs ~{frames_total * cam.image_width_px * cam.image_height_px * 3 / 1e9:.1f} GB raw)")
    report["perception"] = {"detections_sampled": summary.n_detections,
                            "review_fraction": round(summary.review_fraction, 3),
                            "mean_set_size": round(summary.mean_conformal_set_size, 2),
                            "payload_bytes": summary.payload_bytes(),
                            "estimated_full_sortie_detections": int(summary.n_detections * scale)}

    # ------------------------------------------------------------- twin
    rule("7. Crop twin with LAI assimilation")
    weather = make_synthetic_weather(LAT, LON, dt.date(2025, 6, 1), days=200, seed=7)
    pert = ParameterPerturbation(TSUM1=0.18, TSUM2=0.18, SPAN=0.15, RGRLAI=0.18,
                                 TDWI=0.0, WAV=0.15, NAVAILI=0.0)
    flight_days = [dt.date(2025, 7, 20), dt.date(2025, 8, 3), dt.date(2025, 8, 17),
                   dt.date(2025, 8, 31)]
    truth = EnsembleTwin(cfg, weather, n_members=1, perturbation=ParameterPerturbation(0, 0, 0, 0, 0, 0, 0),
                         seed=99, engine="Wofost72_WLP_CWB")
    truth.run([])
    tdf = truth.members[0].output_frame()
    obs = []
    r2 = np.random.default_rng(3)
    for d in flight_days:
        if d in tdf.index and not np.isnan(tdf.loc[d, "LAI"]):
            true_lai = float(tdf.loc[d, "LAI"]) * 1.35        # the twin is biased low
            sd = max(0.05, 0.25 * true_lai)
            obs.append(Observation(d, "LAI", float(true_lai + r2.normal(0, sd)), sd))
    ens = EnsembleTwin(cfg, weather, n_members=a.members, perturbation=pert,
                       seed=5, engine="Wofost72_WLP_CWB")
    res = ens.run(obs)
    mean_y, sd_y, _ = ens.yield_distribution()
    q = ens.quantiles()
    for d, pm, po, ob in zip(res.days, res.prior_mean, res.posterior_mean, obs):
        print(f"  {d}  observed LAI {ob.value:5.2f} +/-{ob.sd:.2f}  "
              f"prior {pm:5.2f} -> posterior {po:5.2f}")
    print(f"  yield  mean {mean_y:.0f} kg/ha, sd {sd_y:.0f}, "
          f"p05 {q[0.05]:.0f} / p50 {q[0.5]:.0f} / p95 {q[0.95]:.0f}")
    print(f"  rejected observations: {sum(res.rejected)}")
    report["twin"] = {"observations": len(obs), "rejected": int(sum(res.rejected)),
                      "yield_mean": round(mean_y), "yield_sd": round(sd_y),
                      "yield_p05": round(q[0.05]), "yield_p95": round(q[0.95])}

    # ------------------------------------------------------------- decision
    rule("8. Risk-aware intervention schedule")
    weathers = [make_synthetic_weather(LAT, LON, dt.date(2025, 6, 1), days=200, seed=s)
                for s in range(a.scenarios)]
    econ = Economics()
    ev = ScenarioEvaluator(cfg, weathers, econ, seed=0, engine="Wofost81_NWLP_CWB_CNB")
    epochs = decision_epochs(dt.date(2025, 7, 5), dt.date(2025, 8, 20), 14)
    beam = beam_search(ev, epochs, beam_width=4, alpha=0.8, max_applications=2)
    best = beam[0]
    print(f"  {ev.n_rollouts} crop-model rollouts over {a.scenarios} scenarios")
    for i, s in enumerate(beam[:3]):
        tag = "BEST" if i == 0 else f"alt{i}"
        print(f"  {tag:5s} CVaR80 {s.cvar:8.0f} {econ.currency}/ha  "
              f"mean {s.expected_value:8.0f}  yield {s.mean_yield:5.0f} kg/ha  {s.rationale}")
    report["decision"] = {"rollouts": ev.n_rollouts, "cvar": round(best.cvar),
                          "mean_margin": round(best.expected_value),
                          "plan": [f"{d:%Y-%m-%d}:{v.amount:g}"
                                   for d, v in sorted(best.actions.items()) if v.kind != "none"]}

    # ------------------------------------------------------------- output
    rule("9. Prescription and compliance gate")
    cells = [PrescriptionCell(zone_id=fid, product_id="urea", rate=v.amount, unit="kg N/ha")
             for v in best.actions.values() if v.kind == "fertilise"]
    rx = Prescription(parcel_id=field.id, valid_from=dt.date(2025, 7, 1),
                      valid_to=dt.date(2025, 9, 30), cells=cells,
                      rationale=best.rationale, model_version="twin-0.1.0+planner-0.1.0",
                      expected_value={"cvar80_per_ha": best.cvar,
                                      "mean_margin_per_ha": best.expected_value})
    APPROVED = {"urea"}
    rx.label_ok = all(c.product_id in APPROVED for c in rx.cells)
    print(f"  {len(rx.cells)} prescription cell(s); label check {'PASS' if rx.label_ok else 'FAIL'}")
    print(f"  attribution: model_version={rx.model_version}")
    log = Log(type=LogType.input, name="Planned N application", status=LogStatus.pending,
              asset_ids=[field.id], location_ids=[field.id], source="planner",
              model_version=rx.model_version,
              quantities=[Quantity(measure=Measure.rate, value=c.rate, unit=c.unit,
                                   label="prescribed", material_id=c.product_id)
                          for c in rx.cells])
    print(f"  log {log.type}/{log.status} with {len(log.quantities)} prescribed quantity(ies)")
    if not rx.label_ok:
        print("  BLOCKED: prescription contains a product not on the approved list")
    report["prescription"] = {"cells": len(rx.cells), "label_ok": rx.label_ok,
                              "log_status": str(log.status)}

    report["wall_clock_s"] = round(time.perf_counter() - t_start, 1)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    rule("Done")
    print(f"  closed loop completed in {report['wall_clock_s']} s with no hardware")
    print(f"  machine-readable report: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
