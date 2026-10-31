"""Region packs: currency, units, prices/costs and the regulation constraint pack.

Rules are *data*, versioned with the code, so a jurisdiction can be added or
corrected without touching the planner. Packs marked ``verified: false`` were
not checked against official texts; every plan made under them carries a
warning.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

REGIONS = Path(__file__).parent / "data" / "regions"


@dataclass(frozen=True)
class Rules:
    verified: bool
    aerial_default_allowed: bool
    wind_max_ms: float
    wind_min_ms: float
    buffer_m: float
    max_speed_ms: float
    max_height_m: float
    weight_threshold_kg: float
    records: tuple[str, ...]
    notes: str
    sources: tuple[str, ...] = ()
    derogation_required: bool = False


@dataclass(frozen=True)
class RegionPack:
    id: str
    name: str
    currency: str
    area_unit: str
    prices: dict = field(hash=False)
    costs: dict = field(hash=False)
    rules: Rules = None  # type: ignore[assignment]
    prices_assumed: bool = True
    prices_note: str = ""
    cost_sources: tuple[str, ...] = ()

    def price(self, crop: str) -> float:
        if crop not in self.prices:
            raise KeyError(f"region {self.id} has no price for {crop!r}; add it to {self.id}.yaml")
        return float(self.prices[crop])

    def warnings(self) -> list[str]:
        w = []
        if not self.rules.verified:
            w.append(f"regulation pack {self.id} is UNVERIFIED -- check local law before flying")
        if not self.rules.aerial_default_allowed:
            w.append(f"aerial spraying is not allowed by default in {self.id}; a derogation is required")
        if self.prices_assumed:
            w.append(f"prices/costs for {self.id} are illustrative assumptions")
        return w


def list_regions() -> list[str]:
    return sorted(p.stem for p in REGIONS.glob("*.yaml"))


@lru_cache(maxsize=None)
def load_region(rid: str) -> RegionPack:
    path = REGIONS / f"{rid}.yaml"
    if not path.exists():
        warnings.warn(f"no region pack {rid!r}; using GLOBAL (unverified)", stacklevel=2)
        path = REGIONS / "GLOBAL.yaml"
    d = yaml.safe_load(path.read_text())
    r = d["rules"]
    rules = Rules(verified=bool(r["verified"]), aerial_default_allowed=bool(r["aerial_default_allowed"]),
                  wind_max_ms=float(r["wind_max_ms"]), wind_min_ms=float(r["wind_min_ms"]),
                  buffer_m=float(r["buffer_m"]), max_speed_ms=float(r["max_speed_ms"]),
                  max_height_m=float(r["max_height_m"]),
                  weight_threshold_kg=float(r["weight_threshold_kg"]),
                  records=tuple(r.get("records", [])), notes=r.get("notes", ""),
                  sources=tuple(r.get("sources", [])),
                  derogation_required=bool(r.get("derogation_required", False)))
    return RegionPack(id=d["id"], name=d["name"], currency=d["currency"], area_unit=d["area_unit"],
                      prices=dict(d["prices"]), costs=dict(d["costs"]), rules=rules,
                      prices_assumed=bool(d.get("prices_assumed", True)),
                      prices_note=d.get("prices_note", ""),
                      cost_sources=tuple(d.get("cost_sources", [])))
