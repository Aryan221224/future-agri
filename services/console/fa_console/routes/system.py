"""Health, metadata (templates, regions), operator settings and data sources."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import __version__
from ..store import now

router = APIRouter(prefix="/api", tags=["system"])
SETTING_CHOICES = {"area_unit": ("ha", "acre", "mu"), "language": ("en",),          # English only
                   "theme": ("auto", "light", "dark")}
SETTING_TEXT = ("operator", "default_region", "aircraft_id")
DEFAULTS = {"area_unit": "ha", "language": "en", "theme": "dark", "operator": "", "default_region": "",
            "aircraft_id": "K3A-01"}

SOURCES = [
    {"layer": "Field boundaries", "source": "Fields of The World global predictions (PRUE)",
     "licence": "CC-BY-4.0", "url": "https://source.coop/ftw"},
    {"layer": "Soil", "source": "SoilGrids 2.0, ISRIC", "licence": "CC-BY-4.0", "url": "https://soilgrids.org"},
    {"layer": "Terrain", "source": "Copernicus DEM GLO-30", "licence": "Copernicus licence (free, attribution)",
     "url": "https://spacedata.copernicus.eu"},
    {"layer": "Weather (observed)", "source": "NASA POWER daily, AG community", "licence": "NASA open data",
     "url": "https://power.larc.nasa.gov"},
    {"layer": "Forecasts", "source": "ECMWF IFS ENS / AIFS ENS, NOAA GEFS via dynamical.org",
     "licence": "CC-BY-4.0", "url": "https://dynamical.org"},
    {"layer": "Satellite", "source": "Sentinel-2 L2A (Copernicus) via Element 84 Earth Search",
     "licence": "Copernicus open data", "url": "https://earth-search.aws.element84.com/v1"},
    {"layer": "Crop model", "source": "PCSE / WOFOST 7.2, Wageningen University & Research",
     "licence": "EUPL-1.2", "url": "https://pcse.readthedocs.io"},
    {"layer": "Weed detector", "source": "RF-DETR fine-tuned on WeedsGalore", "licence": "Apache-2.0 / CC-BY-4.0",
     "url": "https://github.com/roboflow/rf-detr"},
    {"layer": "Base map", "source": "OpenStreetMap contributors", "licence": "ODbL",
     "url": "https://www.openstreetmap.org/copyright"},
    {"layer": "Satellite base map", "source": "Sentinel-2 cloudless 2016 by EOX IT Services GmbH",
     "licence": "CC-BY-4.0", "url": "https://s2maps.eu"},
]


@router.get("/health")
def health(request: Request) -> dict:
    s = request.app.state
    return {"ok": True, "version": __version__, "time": now(), "lan_exposed": s.settings.lan_exposed,
            "offline": s.settings.offline, "workers": s.jobs.workers()}


@router.get("/meta")
def meta(request: Request) -> dict:
    from fa_drone.spec import load_spec
    from fa_farm.archetypes import load_archetypes
    from fa_farm.regions import list_regions, load_region
    templates = []
    for a in load_archetypes().values():
        templates.append({
            "id": a.id, "name": a.name, "crop": a.crop, "variety": a.variety, "region": a.region,
            "irrigated": a.irrigated, "weeds": a.weeds, "diseases": list(a.diseases),
            "field_ha": list(a.field_ha), "sowing": a.sowing, "harvest": a.harvest,
            "supported": a.supported, "unsupported_reason": a.unsupported_reason,
            "drone_use": a.drone_use, "fidelity_note": a.fidelity_note,
            "locations": [{"name": loc.name, "lat": loc.lat, "lon": loc.lon, "country": loc.country}
                          for loc in a.locations]})
    regions = []
    for rid in list_regions():
        r = load_region(rid)
        regions.append({"id": r.id, "name": r.name, "currency": r.currency, "verified": r.rules.verified,
                        "prices_assumed": r.prices_assumed, "area_unit": r.area_unit,
                        "aerial_default_allowed": r.rules.aerial_default_allowed,
                        "wind_max_ms": r.rules.wind_max_ms, "buffer_m": r.rules.buffer_m,
                        "max_height_m": r.rules.max_height_m, "records": list(r.rules.records),
                        "warnings": r.warnings()})
    return {"version": __version__, "templates": templates, "regions": regions,
            "job_kinds": request.app.state.jobs.kinds(), "drone": load_spec().name,
            "lan_exposed": request.app.state.settings.lan_exposed}


@router.get("/settings")
def get_settings(request: Request) -> dict:
    stored = {k: v for k, v in request.app.state.store.settings().items()
              if k in DEFAULTS and (k not in SETTING_CHOICES or v in SETTING_CHOICES[k])}   # drops retired values
    return {**DEFAULTS, **stored}


@router.post("/settings")
def set_settings(body: dict, request: Request) -> dict:
    st = request.app.state.store
    for k, v in body.items():
        if k in SETTING_CHOICES:
            if v not in SETTING_CHOICES[k]:
                raise ValueError(f"{k} must be one of {SETTING_CHOICES[k]}")
        elif k in SETTING_TEXT:
            if not isinstance(v, str) or len(v) > 80:
                raise ValueError(f"{k} must be text of at most 80 characters")
        else:
            raise ValueError(f"unknown setting '{k}'")
    for k, v in body.items():
        st.set_setting(k, v)
    st.add_event(None, "settings", body)
    return get_settings(request)


@router.get("/sources")
def sources() -> dict:
    from fa_runner.report import ATTRIBUTION
    return {"sources": SOURCES, "attribution": ATTRIBUTION}


@router.get("/audit")
def audit(request: Request, farm_id: str | None = None, limit: int = 200) -> dict:
    return {"events": request.app.state.store.events(farm_id, limit=min(limit, 2000))}
