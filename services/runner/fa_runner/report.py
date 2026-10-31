"""Run reports: JSON (everything), CSV (ranked tables), GeoJSON (field, zones,
last as-applied dose), KML + settings sheet for JIYI Agri Assistant, and the
attribution every CC-BY source requires."""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
from pathlib import Path

import numpy as np

ATTRIBUTION = [
    "Field boundaries: Fields of The World global predictions (Taylor Geospatial Institute, Microsoft AI for Good), CC-BY-4.0",
    "Soil: SoilGrids 2.0, ISRIC - World Soil Information, CC-BY-4.0",
    "Terrain: Copernicus DEM GLO-30 (c) DLR e.V. / Airbus, provided under COPERNICUS by the EU and ESA",
    "Weather: NASA Langley Research Center POWER Project",
    "Forecasts: ECMWF IFS/AIFS ENS and NOAA GEFS, processed by dynamical.org, CC-BY-4.0",
    "Satellite: contains modified Copernicus Sentinel data (ESA), via Element 84 Earth Search",
    "Detector: RF-DETR (Apache-2.0) fine-tuned on WeedsGalore (Celikkan et al., WACV 2025, CC-BY-4.0)",
    "Crop model: PCSE/WOFOST (EUPL-1.2), Wageningen University & Research",
]


def _default(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if hasattr(o, "as_dict"):
        return o.as_dict()
    return str(o)


def write_report(results: dict, vf, out_dir, spec=None, settings=None) -> dict:
    """``results``: {name: SeasonResult | PerfectRun | list}. -> {kind: path}."""
    from fa_drone.jiyi_io import export_kml, settings_sheet
    from fa_drone.mission import MissionSettings
    from fa_drone.spec import load_spec
    from fa_farm.grid import FarmGrid
    from fa_farm.regions import load_region

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    spec = spec or load_spec()
    region = load_region(vf.spec.archetype.region)
    warnings = list(vf.warnings) + region.warnings()
    payload = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(), "farm": vf.summary(),
               "results": results, "attribution": ATTRIBUTION, "warnings": warnings,
               "not_field_validated": ("All results are simulated on a virtual farm built from real "
                                       "data; none is a field measurement.")}
    pj = out / "report.json"
    pj.write_text(json.dumps(payload, default=_default, indent=1))

    # CSV: one row per season result found anywhere in results
    rows = []
    for name, r in results.items():
        items = r if isinstance(r, list) else [r]
        for it in items:
            if hasattr(it, "summary"):
                s = it.summary()
                rows.append({"name": name, **{k: v for k, v in s.items() if not isinstance(v, (dict, list))}})
    pc = out / "results.csv"
    with pc.open("w", newline="") as f:
        if rows:
            keys = sorted({k for r in rows for k in r})
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        else:
            f.write("name\n")

    # GeoJSON: field polygon + cells with the last as-applied dose of the first result
    grid = FarmGrid.from_wgs84(vf.spec.polygon_wgs84, vf.spec.cell_m)
    feats = [{"type": "Feature", "properties": {"kind": "field", "area_ha": round(grid.area_ha, 3)},
              "geometry": vf.spec.polygon_wgs84.__geo_interface__}]
    first = next((it for r in results.values() for it in (r if isinstance(r, list) else [r])
                  if getattr(it, "as_applied_last", None)), None)
    if first is not None:
        ll = grid.cells_lonlat()
        for (lon, lat), v in zip(ll, first.as_applied_last):
            feats.append({"type": "Feature", "properties": {"kind": "as_applied_dose_frac", "v": v},
                          "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]}})
    pg = out / "field.geojson"
    pg.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))

    pk = export_kml(vf.spec.polygon_wgs84, [], f"{vf.spec.archetype.id} {vf.spec.site.name}",
                    out / "field_for_jiyi.kml")
    sheet = settings_sheet(settings or MissionSettings(), spec, region)
    ps = out / "jiyi_settings.json"
    ps.write_text(json.dumps(sheet, indent=1, default=_default))
    return {"json": str(pj), "csv": str(pc), "geojson": str(pg), "kml": str(pk), "settings": str(ps)}
