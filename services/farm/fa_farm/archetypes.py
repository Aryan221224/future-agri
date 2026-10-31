"""Archetype farms (research brief, 31 Oct 2026) as typed data."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

DATA = Path(__file__).parent / "data"


@dataclass(frozen=True)
class Site:
    name: str
    lat: float
    lon: float
    country: str


@dataclass(frozen=True)
class Archetype:
    id: str
    name: str
    locations: tuple[Site, ...]
    field_ha: tuple[float, float]
    crop: str
    variety: str
    sowing: str
    harvest: str
    irrigated: bool
    diseases: tuple[str, ...]
    pests: tuple[str, ...]
    weeds: bool
    region: str
    drone_use: str
    sources: tuple[str, ...]
    fidelity_note: str = ""
    supported: bool = True
    unsupported_reason: str = ""
    extra: dict = field(default_factory=dict, compare=False, hash=False)


@lru_cache(maxsize=1)
def load_archetypes() -> dict[str, Archetype]:
    raw = yaml.safe_load((DATA / "archetypes.yaml").read_text())
    out = {}
    for aid, d in raw.items():
        out[aid] = Archetype(
            id=aid, name=d["name"],
            locations=tuple(Site(**s) for s in d["locations"]),
            field_ha=(float(d["field_ha"][0]), float(d["field_ha"][1])),
            crop=d["crop"], variety=str(d["variety"]), sowing=d["sowing"], harvest=d["harvest"],
            irrigated=bool(d["irrigated"]), diseases=tuple(d.get("diseases", [])),
            pests=tuple(d.get("pests", [])), weeds=bool(d.get("weeds", True)),
            region=d["region"], drone_use=d.get("drone_use", ""),
            sources=tuple(d.get("sources", [])), fidelity_note=d.get("fidelity_note", ""),
            supported=bool(d.get("supported", True)),
            unsupported_reason=d.get("unsupported_reason", ""))
    return out


def season_dates(arch: Archetype, year: int) -> tuple[dt.date, dt.date]:
    """(sowing, harvest) for the season that starts in ``year``. A harvest month
    earlier than the sowing month falls in the next calendar year (southern
    hemisphere summer crops, tropical dry-season crops)."""
    sm, sd = (int(x) for x in arch.sowing.split("-"))
    hm, hd = (int(x) for x in arch.harvest.split("-"))
    sow = dt.date(year, sm, sd)
    hy = year + 1 if (hm, hd) <= (sm, sd) else year
    return sow, dt.date(hy, hm, hd)
