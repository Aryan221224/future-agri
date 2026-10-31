#!/usr/bin/env python3
"""Plan the initial camera campaign for a field.

Produces everything needed to actually walk the field:
  - a printable field sheet with the station list and the capture protocol
  - station coordinates as GeoJSON, to load on a phone GPS app
  - a validation pass that refuses a campaign the twin cannot use

Usage:
    python3 scripts/plan_campaign.py --lon 75.85 --lat 30.90 \
        --width-m 700 --height-m 420 --crop rice --variety Rice_501 \
        --start 2025-07-01 --out-dir campaigns/plot12
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/fa_core", "services/gateway", "services/twin"):
    sys.path.insert(0, str(ROOT / p))

from shapely.geometry import Polygon, mapping  # noqa: E402

from fa_core.geo import area_ha  # noqa: E402
from fa_core.geoid import geoid  # noqa: E402
from fa_gateway.campaign import build_campaign, stations_needed  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lon", type=float, required=True, help="SW corner longitude")
    ap.add_argument("--lat", type=float, required=True, help="SW corner latitude")
    ap.add_argument("--width-m", type=float, default=700.0)
    ap.add_argument("--height-m", type=float, default=420.0)
    ap.add_argument("--boundary", type=Path,
                    help="GeoJSON polygon file; overrides the rectangle above")
    ap.add_argument("--crop", default="rice")
    ap.add_argument("--variety", default="Rice_501")
    ap.add_argument("--start", type=dt.date.fromisoformat, required=True,
                    help="date of the first visit, YYYY-MM-DD")
    ap.add_argument("--rounds", type=int, default=3,
                    help="capture rounds; 3 is where the benefit plateaus")
    ap.add_argument("--span-days", type=int, default=21)
    ap.add_argument("--stations", type=int, default=None,
                    help="override the computed station count")
    ap.add_argument("--field-cv", type=float, default=0.20,
                    help="expected spatial CV of LAI; sizes the station count")
    ap.add_argument("--precision", type=float, default=0.10,
                    help="target relative precision on field-mean LAI")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "campaigns" / "default")
    a = ap.parse_args()

    if a.boundary:
        gj = json.loads(a.boundary.read_text(encoding="utf-8"))
        geom = gj["geometry"] if gj.get("type") == "Feature" else gj
        from shapely.geometry import shape
        field = shape(geom)
    else:
        m_per_deg = 111_320.0
        dw = a.width_m / (m_per_deg * np.cos(np.radians(a.lat)))
        dh = a.height_m / m_per_deg
        field = Polygon([(a.lon, a.lat), (a.lon + dw, a.lat),
                         (a.lon + dw, a.lat + dh), (a.lon, a.lat + dh)])

    fid = geoid(field)
    n = a.stations or stations_needed(a.field_cv, a.precision)
    plan = build_campaign(field, fid, a.crop, a.variety, a.start,
                          n_stations=n, n_rounds=a.rounds, span_days=a.span_days,
                          cv=a.field_cv, precision=a.precision)

    problems = plan.validate()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "field_sheet.txt").write_text(plan.field_sheet(), encoding="utf-8")
    (a.out_dir / "stations.geojson").write_text(json.dumps(plan.as_geojson(), indent=2), encoding="utf-8")
    (a.out_dir / "boundary.geojson").write_text(json.dumps(
        {"type": "Feature", "properties": {"geoid": fid}, "geometry": mapping(field)},
        indent=2), encoding="utf-8")
    (a.out_dir / "campaign.json").write_text(json.dumps({
        "geoid": fid, "area_ha": round(plan.area_ha, 3),
        "crop": a.crop, "variety": a.variety,
        "stations": [s.__dict__ for s in plan.stations],
        "visits": [{"round": v.round_no, "date": str(v.planned_date),
                    "window": f"{v.window_start:%H:%M}-{v.window_end:%H:%M}",
                    "note": v.note} for v in plan.visits],
        "captures_total": plan.total_captures,
        "validation": problems or "ok",
    }, indent=2, default=str), encoding="utf-8")

    print(f"field   {fid}  ({plan.area_ha:.2f} ha, {a.crop}/{a.variety})")
    print(f"sizing  field CV {a.field_cv:.0%}, target precision +/-{a.precision:.0%} "
          f"-> {len(plan.stations)} stations")
    print(f"visits  {len(plan.visits)} rounds over "
          f"{(plan.visits[-1].planned_date - plan.visits[0].planned_date).days} days")
    for v in plan.visits:
        print(f"          round {v.round_no} ({v.note:8s}) {v.planned_date:%a %d %b %Y} "
              f"{v.window_start:%H:%M}-{v.window_end:%H:%M}")
    print(f"effort  {plan.total_captures} captures total, "
          f"~{plan.estimated_field_time_min():.0f} min per visit")
    print(f"\nvalidation: {'PASS' if not problems else 'FAIL'}")
    for p in problems:
        print(f"  ! {p}")
    print(f"\nwritten to {a.out_dir}/")
    for f in sorted(a.out_dir.iterdir()):
        print(f"  {f.name}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
